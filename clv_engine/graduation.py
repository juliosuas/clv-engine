"""Measured-sample status and the paper->real graduation gate (REPORTING ONLY).

Nothing in here, or anywhere in this package, places orders or touches a wallet: the gate only prints
whether the paper evidence meets a set of *proposed* thresholds. The thresholds are proposals for Julio
to approve (see docs/GRADUATION.md); they are not facts and not recommendations to trade.

Unit of evidence = the GAME. Entries of one game (both sides, both venues, repeated entries on one outcome) are
correlated, so each game contributes ONE number (the mean CLV of its measured entries), the t interval and the
bootstrap both run over those game means, and "n measured" is the number of distinct games.

Sample ("measured entries"): paper entries that
  * were priced against Pinnacle (ref_source='pinnacle') and have a captured Pinnacle close (clv_status='done'),
  * are on a real venue (polymarket / kalshi; soft-book entries are a different market),
  * were filled in full (partial=0),
  * and would pass the CURRENT entry rules: league / venue not excluded, edge_entry >= MIN_EDGE.
CLV is after fees (effective_price). The CI treats all entries of one game as one cluster (an entry on
both sides, or on both venues, of the same game is not independent evidence).
"""

from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import Any

from .ledger import ALT_CLOSE_METHODS, Ledger, alt_close_source
from .util import parse_datetime, utc_now

MIN_RATE_ENTRIES = 10  # fewer measured games than this: the games/day rate is not estimated
MIN_RATE_DAYS = 2.0    # observation span shorter than this: entries/day is not estimated
MIN_CI_ENTRIES = 5

# PROPOSED real-phase limits, for Julio to approve or change. Informational only: no code enforces
# or acts on them because the repo has no trading code.
# Rules fixed BEFORE measuring (do not change them after looking at results).
PREREGISTERED_RULES: dict[str, str] = {
    "close": "last Pinnacle no-vig sample actually captured with timestamp <= scheduled kickoff (the last valid sample if Pinnacle "
             "pulls the market); 'valid' = fetched within CLOSE_MAX_AGE_MINUTES of kickoff. No such sample -> entry marked "
             "pending_sharp, then no_close after SETTLE_AFTER_HOURS, and excluded from the measured sample (never imputed)",
    "clv": "all-in entry price (average fill + taker fee) vs the fair no-vig close: close / effective_price - 1, same fee in both "
           "the edge and the CLV; headline uses DEVIG_METHOD (default power); power and multiplicative are both reported",
    "fills": "paper entries are always modelled as TAKER fills walked down the real ask book, full fill required; resting/maker "
             "fills are never simulated as certain",
    "unit": "one observation per game (entries of a game are correlated); n measured = distinct games",
}

PROPOSED_RISK_LIMITS: dict[str, str] = {
    "bankroll": "small, an amount Julio can lose entirely; he sets the number (not chosen here)",
    "max_per_ticket": "1% of bankroll",
    "daily_stop_loss": "proposal: 3% of bankroll realized loss in a UTC day, then stop until the next day",
    "total_stop_loss": "proposal: 20% drawdown from the starting bankroll, then stop and re-review",
    "kill_switch": "one flag (file or env) that halts any real-money activity immediately, checked before every ticket; plus manual off",
    "scope": "only venues/leagues that individually clear the gate; start with the single smallest ticket size",
}


# ---- statistics (stdlib only) -------------------------------------------------
def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def t_quantile(p: float, df: float) -> float:
    """Student-t quantile via the Cornish-Fisher expansion (Abramowitz & Stegun 26.7.5); fine for df >= 4."""
    z = NormalDist().inv_cdf(p)
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    g4 = (79 * z**9 + 776 * z**7 + 1482 * z**5 - 1920 * z**3 - 945 * z) / 92160
    return z + g1 / df + g2 / df**2 + g3 / df**3 + g4 / df**4


def t_interval(xs: list[float], level: float = 0.90) -> tuple[float, float] | None:
    """Two-sided t interval for the mean, treating entries as independent (optimistic when games repeat)."""
    n = len(xs)
    if n < MIN_CI_ENTRIES:
        return None
    m = mean(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    half = t_quantile((1 + level) / 2, n - 1) * sd / math.sqrt(n)
    return m - half, m + half


def cluster_bootstrap(groups: list[list[float]], level: float = 0.90, samples: int = 5000, seed: int = 20261027) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the entry-level mean, resampling whole games (clusters). Deterministic seed."""
    g = [x for x in groups if x]
    if len(g) < MIN_CI_ENTRIES:
        return None
    sums = [sum(x) for x in g]
    cnts = [len(x) for x in g]
    rng = random.Random(seed)
    idx = range(len(g))
    means = []
    for _ in range(samples):
        pick = rng.choices(idx, k=len(g))
        means.append(sum(sums[i] for i in pick) / sum(cnts[i] for i in pick))
    means.sort()
    lo_i = int(((1 - level) / 2) * (samples - 1))
    hi_i = int(((1 + level) / 2) * (samples - 1))
    return means[lo_i], means[hi_i]


# ---- sample selection -----------------------------------------------------
def measured_sample(ledger: Ledger, settings) -> list[Any]:
    rows = ledger.entries(
        "ref_source='pinnacle' AND clv_status='done' AND clv_pct IS NOT NULL AND sharp_fair_close IS NOT NULL "
        "AND venue IN ('polymarket','kalshi') AND partial=0"
    )
    return [r for r in rows
            if r["league"] not in settings.excluded_leagues and r["venue"] not in settings.excluded_venues
            and r["edge_entry"] >= settings.min_edge - 1e-9]


def game_units(rows: list[Any], key: str | None = None, value=lambda r: r["clv_pct"]) -> dict[Any, float]:
    """One number per game (or per (group, game)): the mean of the entries' values. Entries of a game are correlated."""
    acc: dict[Any, list[float]] = {}
    for r in rows:
        acc.setdefault((r[key], r["game_key"]) if key else r["game_key"], []).append(value(r))
    return {k: mean(v) for k, v in acc.items() if v}


def _group_units(rows: list[Any], key: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for (grp, _game), m in game_units(rows, key).items():
        out.setdefault(grp, []).append(m)
    return out


def _intervals(units: list[float], settings) -> dict[str, Any]:
    n = len(units)
    t = t_interval(units, settings.grad_ci_level)
    sd = math.sqrt(sum((x - mean(units)) ** 2 for x in units) / (n - 1)) if n > 1 else None
    return {
        "n_games": n,
        "t": t,
        "bootstrap": cluster_bootstrap([[x] for x in units], settings.grad_ci_level, settings.grad_bootstrap_samples),
        "t_stat": (mean(units) / (sd / math.sqrt(n))) if sd else None,
    }


def sensitivity(ledger: Ledger, rows: list[Any], settings) -> dict[str, Any]:
    """CLV recomputed against the same Pinnacle close de-vigged with each method (power / multiplicative).
    Entries without the alternative close rows (captured before they existed, or backfilled) are not counted."""
    out: dict[str, Any] = {}
    for method in ALT_CLOSE_METHODS:
        vals: list[tuple[Any, float]] = []
        for r in rows:
            c = ledger.closing(r["game_key"], r["outcome_key"], alt_close_source(method))
            if c is None:
                continue
            c = c if r["side"] == "yes" else 1.0 - c
            vals.append((r, c / r["effective_price"] - 1))
        by_game: dict[str, list[float]] = {}
        for r, v in vals:
            by_game.setdefault(r["game_key"], []).append(v)
        units = [mean(v) for v in by_game.values()]
        out[method] = {"entries": len(vals), "games": len(units), "mean_clv": mean(units) if units else None,
                       "ci": t_interval(units, settings.grad_ci_level)}
    return out


def _observation_start(ledger: Ledger) -> datetime | None:
    r = ledger.conn.execute("SELECT MIN(ts) FROM runs").fetchone()
    cands = [parse_datetime(r[0])] if r and r[0] else []
    r = ledger.conn.execute("SELECT MIN(created_at) FROM entries").fetchone()
    if r and r[0]:
        cands.append(parse_datetime(r[0]))
    return min((c for c in cands if c), default=None)


def retro_rate(ledger: Ledger, settings, now: datetime) -> dict[str, Any]:
    """Entries/day the CURRENT filters would have produced on the ledger's history (retroactive; reads only).
    Re-applied: Pinnacle reference, full fill, venue is polymarket/kalshi, MIN_EDGE, league/venue exclusions.
    NOT re-appliable: MIN_DEPTH_USD (only the filled levels were stored), so this is an UPPER bound on that filter."""
    rows = [r for r in ledger.entries("ref_source='pinnacle' AND venue IN ('polymarket','kalshi') AND partial=0")
            if r["league"] not in settings.excluded_leagues and r["venue"] not in settings.excluded_venues
            and r["edge_entry"] >= settings.min_edge - 1e-9]
    start = _observation_start(ledger)
    days = (now - start).total_seconds() / 86400 if start else 0.0
    games = len({r["game_key"] for r in rows})
    out: dict[str, Any] = {"entries": len(rows), "games": games, "observed_days": days, "entries_per_day": None, "games_per_day": None,
                           "note": "MIN_DEPTH_USD cannot be re-applied to old rows (upper bound)"}
    if start is None or days < MIN_RATE_DAYS:
        out["estimate"] = f"insufficient data: ledger history is {days:.1f} days (< {MIN_RATE_DAYS:g})"
    else:
        out["entries_per_day"], out["games_per_day"] = len(rows) / days, games / days
        out["estimate"] = f"{len(rows)} entries in {games} games over {days:.1f} days pass the current filters"
    return out


def _deadline_warning(st: dict[str, Any], settings, now: datetime) -> str:
    target, n = settings.grad_min_games, st["games_measured"]
    try:
        dl = date.fromisoformat(settings.grad_deadline)
    except ValueError:
        return ""
    days_left = (dl - now.date()).days
    if n >= target:
        return ""
    need = target - n
    if days_left <= 0:
        return f"WARNING: deadline {dl} has passed with {n}/{target} measured games."
    req = need / days_left
    gpd = st["games_per_day"]
    basis = "measured"
    if gpd is None and st["retro"]["games_per_day"]:
        gpd, basis = st["retro"]["games_per_day"], "retroactive (optimistic: assumes every entry gets a close)"
    if gpd is None or gpd <= 0:
        return (f"WARNING: cannot confirm {target} measured games by {dl}: no usable rate yet ({n}/{target}, {days_left} days left, "
                f"would need {req:.2f} games/day).")
    eta_days = need / gpd
    if eta_days > days_left:
        eta = (now + timedelta(days=eta_days)).date()
        return (f"WARNING: pace too slow for {target} measured games by {dl}: {gpd:.2f} games/day ({basis}) reaches it ~{eta}; "
                f"need {req:.2f} games/day.")
    return ""


# ---- status -----------------------------------------------------------------
def status(ledger: Ledger, settings, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    allrows = ledger.entries()
    sample = measured_sample(ledger, settings)
    units = game_units(sample)
    gm = list(units.values())
    settled = [r for r in sample if r["status"] == "settled"]
    iv = _intervals(gm, settings) if gm else {"n_games": 0, "t": None, "bootstrap": None, "t_stat": None}
    out: dict[str, Any] = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries_total": len(allrows),
        "entries_with_pinnacle_close": sum(1 for r in allrows if r["sharp_fair_close"] is not None),
        "pending_close": sum(1 for r in allrows if r["clv_status"] == "pending_close"),
        "pending_sharp": sum(1 for r in allrows if r["clv_status"] == "pending_sharp"),
        "no_close": sum(1 for r in allrows if r["clv_status"] == "no_close"),
        "settled_total": sum(1 for r in allrows if r["status"] == "settled"),
        "games_measured": len(gm),
        "sample_n": len(sample),
        "sample_settled": len(settled),
        "mean_clv": mean(gm) if gm else None,  # mean of per-game means
        "mean_clv_entry_weighted": mean([r["clv_pct"] for r in sample]) if sample else None,
        "beat_close": (sum(1 for x in gm if x > 0) / len(gm)) if gm else None,  # share of games
        "ci_level": settings.grad_ci_level,
        "ci": iv,
        "sensitivity": sensitivity(ledger, sample, settings) if sample else {m: {"entries": 0, "games": 0, "mean_clv": None, "ci": None} for m in ALT_CLOSE_METHODS},
        "devig_method": settings.devig_method,
        "target": settings.grad_min_games,
        "deadline": settings.grad_deadline,
        "entries_per_day": None,
        "games_per_day": None,
        "days_to_target": None,
        "projection": "",
        "retro": retro_rate(ledger, settings, now),
    }
    first = min((parse_datetime(r["created_at"]) for r in sample), default=None)
    span = (now - first).total_seconds() / 86400 if first else 0.0
    out["observed_days"] = span
    if len(gm) >= settings.grad_min_games:
        out["days_to_target"] = 0.0
        out["projection"] = f"target of {settings.grad_min_games} measured games reached"
        if span > 0:
            out["entries_per_day"], out["games_per_day"] = len(sample) / span, len(gm) / span
    elif len(gm) < MIN_RATE_ENTRIES or span < MIN_RATE_DAYS:
        out["projection"] = (f"insufficient data: need >= {MIN_RATE_ENTRIES} measured games over >= {MIN_RATE_DAYS:g} days to estimate a rate "
                             f"(have {len(gm)} games over {span:.1f} days)")
    else:
        out["entries_per_day"], out["games_per_day"] = len(sample) / span, len(gm) / span
        out["days_to_target"] = (settings.grad_min_games - len(gm)) / out["games_per_day"]
        out["projection"] = (f"~{out['days_to_target']:.0f} days to {settings.grad_min_games} measured games at the observed rate "
                             "(naive: assumes the rate holds)")
    out["deadline_warning"] = _deadline_warning(out, settings, now)
    usage = ledger.month_usage(now.strftime("%Y-%m"))
    out["quota_month"] = {"credits": usage["credits"], "requests": usage["requests"], "skipped": usage["skipped"],
                          "remaining": usage["remaining"], "used": usage["used"], "budget": settings.odds_api_monthly_budget,
                          "hard_cap": settings.odds_api_hard_cap, "missed_closes": len(ledger.missed_closes())}
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def _ci_txt(ci: tuple[float, float] | None) -> str:
    return "n/a (too few games)" if ci is None else f"[{ci[0]:+.2%}, {ci[1]:+.2%}]"


def format_status(st: dict[str, Any]) -> str:
    q, rt = st["quota_month"], st["retro"]
    lvl = f"{st['ci_level']:.0%}"
    beat = "n/a" if st["beat_close"] is None else f"{st['beat_close']:.1%}"
    tstat = "n/a" if st["ci"]["t_stat"] is None else f"{st['ci']['t_stat']:.2f}"
    sens = " | ".join(f"{m}: {_pct(v['mean_clv'])} (n={v['games']} games)" for m, v in st["sensitivity"].items())
    lines = [
        f"CLV STATUS (paper only, as of {st['as_of']})",
        f"entries total: {st['entries_total']} | with captured Pinnacle close: {st['entries_with_pinnacle_close']} | "
        f"pending close: {st['pending_close']} | pending Pinnacle close: {st['pending_sharp']} | NO CLOSE (marked, excluded): {st['no_close']} | "
        f"settled: {st['settled_total']}",
        f"MEASURED GAMES (distinct games; entries of one game are correlated): {st['games_measured']} of target {st['target']} "
        f"({st['sample_n']} entries, {st['sample_settled']} settled)",
        f"mean CLV after fees, per-game mean ({st['devig_method']} de-vig): {_pct(st['mean_clv'])} | t-stat {tstat} | games beating the close: {beat}",
        f"{lvl} CI, bootstrap over games: {_ci_txt(st['ci']['bootstrap'])} | {lvl} CI, t over games: {_ci_txt(st['ci']['t'])}",
        f"sensitivity to de-vig method (same closes): {sens}",
        "measured games/day: " + ("n/a" if st["games_per_day"] is None else f"{st['games_per_day']:.2f}")
        + " | entries/day: " + ("n/a" if st["entries_per_day"] is None else f"{st['entries_per_day']:.2f}") + f" over {st['observed_days']:.1f} days",
        f"projection: {st['projection']}",
        "under the CURRENT entry filters, retroactively on the ledger history: " + rt["estimate"]
        + ("" if rt["entries_per_day"] is None else f" => {rt['entries_per_day']:.2f} entries/day, {rt['games_per_day']:.2f} games/day")
        + f" ({rt['note']})",
    ]
    if st["deadline_warning"]:
        lines.append(st["deadline_warning"])
    lines.append(f"Odds API credits this month (local count, UTC month): {q['credits']:.0f} in {q['requests']} calls (hard cap {q['hard_cap']} of {q['budget']}), "
                 f"{q['skipped']} polls skipped by budget, {q['missed_closes']} close round(s) lost; "
                 f"last x-requests-used/remaining: {'n/a' if q['used'] is None else q['used']}/{'n/a' if q['remaining'] is None else q['remaining']}")
    return "\n".join(lines)


# ---- graduation gate -----------------------------------------------------------
def _loo_ok(groups: dict[str, list[float]], min_n: int) -> bool:
    if len(groups) < 2:
        return True
    for g in groups:
        rest = [c for k, v in groups.items() if k != g for c in v]
        if len(rest) < min_n or mean(rest) <= 0:
            return False
    return True


def graduation(ledger: Ledger, settings, now: datetime | None = None) -> dict[str, Any]:
    """Three outcomes: PASS, FAIL, or EXTEND (not enough evidence yet: collect more, this is NOT a failure).
    The gate is only decided at >= GRAD_MIN_GAMES measured games; before that it is always EXTEND (no peeking)."""
    now = now or utc_now()
    sample = measured_sample(ledger, settings)
    units = game_units(sample)
    gm = list(units.values())
    n = len(gm)
    need = settings.grad_min_games
    crit: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        crit.append({"name": name, "ok": ok, "detail": detail})

    add("sample size", n >= need or None, f"{n} measured games, {len(sample)} entries (need >= {need} distinct games)")
    m = mean(gm) if gm else None
    iv = _intervals(gm, settings) if gm else {"t": None, "bootstrap": None, "t_stat": None}
    add("mean CLV > 0", None if n < MIN_CI_ENTRIES else m > 0, f"mean of per-game mean CLV {_pct(m)} (after fees, {settings.devig_method} de-vig)")
    lowers = [ci[0] for ci in (iv["bootstrap"], iv["t"]) if ci]
    lvl = f"{settings.grad_ci_level:.0%}"
    add(f"{lvl} CI lower bound > 0 (both methods)", None if len(lowers) < 2 else min(lowers) > 0,
        f"bootstrap over games {_ci_txt(iv['bootstrap'])}; t over games {_ci_txt(iv['t'])}")
    settled = [r for r in sample if r["status"] == "settled"]
    pnl = sum(r["pnl"] or 0 for r in settled)
    cost = sum(r["stake"] + r["fee"] for r in settled)
    enough_settled = bool(sample) and len(settled) >= settings.grad_min_settled_fraction * len(sample)
    add("after-fee paper P&L > 0 on the same sample", None if not enough_settled else pnl > 0,
        f"P&L ${pnl:,.2f} on ${cost:,.2f} staked ({len(settled)}/{len(sample)} entries settled; need >= {settings.grad_min_settled_fraction:.0%} settled)")
    by_league = _group_units(sample, "league")
    big = {k: v for k, v in by_league.items() if len(v) >= settings.grad_min_group_games}
    top_share = (max(len(v) for v in by_league.values()) / n) if n else None
    league_ok = (len(big) >= settings.grad_min_leagues and top_share <= settings.grad_max_league_share
                 and _loo_ok(by_league, settings.grad_min_group_games) and all(mean(v) > 0 for v in big.values())) if n else None
    top_txt = "n/a" if top_share is None else f"{top_share:.0%}"
    by_txt = ", ".join(f"{k} {_pct(mean(v))} (n={len(v)} games)" for k, v in sorted(by_league.items())) or "n/a"
    add("no single league drives the result", league_ok,
        f"leagues with >= {settings.grad_min_group_games} games: {len(big)} (need >= {settings.grad_min_leagues}); top league share {top_txt} "
        f"(max {settings.grad_max_league_share:.0%}); mean CLV by league: {by_txt}; leave-one-league-out mean CLV > 0 required")
    by_venue = _group_units(sample, "venue")
    bigv = {k: v for k, v in by_venue.items() if len(v) >= settings.grad_min_group_games}
    venue_ok = (len(bigv) >= settings.grad_min_venues and all(mean(v) > 0 for v in bigv.values()) and _loo_ok(by_venue, settings.grad_min_group_games)) if n else None
    vtxt = ", ".join(f"{k} {_pct(mean(v))} (n={len(v)} games)" for k, v in sorted(by_venue.items())) or "n/a"
    add("no single venue drives the result", venue_ok,
        f"venues with >= {settings.grad_min_group_games} games: {len(bigv)} (need >= {settings.grad_min_venues}); mean CLV by venue: {vtxt}; "
        f"excluded venues now: {','.join(settings.excluded_venues) or '-'}")

    met = sum(1 for c in crit if c["ok"] is True)
    if n < need:
        state = "extend"
        verdict = (f"INSUFFICIENT SAMPLE: EXTEND collection ({n}/{need} measured games). This is not a failure; the gate is only decided at "
                   f">= {need} games, so early looks cannot pass or fail it.")
    elif any(c["ok"] is False for c in crit):
        state = "fail"
        verdict = f"FAIL: sample is large enough ({n} games) and {sum(1 for c in crit if c['ok'] is False)} criteria are not met."
    elif any(c["ok"] is None for c in crit):
        state = "extend"
        verdict = "EXTEND: sample size is reached but some criteria cannot be decided yet (e.g. too few settled entries); wait, do not change the rules."
    else:
        state = "pass"
        verdict = ("PASS on paper. This is evidence for a decision by Julio, not an authorization: nothing in this repository places real orders, "
                   "and any real-money step needs his explicit approval plus separately reviewed code.")
    return {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": state,
        "passed": state == "pass",
        "met": met,
        "total": len(crit),
        "criteria": crit,
        "sample_n": len(sample),
        "games_measured": n,
        "rules": PREREGISTERED_RULES,
        "proposed_risk_limits": PROPOSED_RISK_LIMITS,
        "verdict": verdict,
    }


def format_graduation(g: dict[str, Any]) -> str:
    below = g["state"] == "extend" and not g["criteria"][0]["ok"]
    mark = {True: "PASS", False: "FAIL", None: "N/A "}
    lines = [f"GRADUATION CHECK (paper -> real; proposed thresholds, for Julio to approve; as of {g['as_of']})"]
    for c in g["criteria"]:
        tag = mark[c["ok"]] + ("?" if below and c["ok"] is not None else "")
        lines.append(f"[{tag}] {c['name']}: {c['detail']}")
    if below:
        lines.append("(PASS?/FAIL? = provisional reading on an undersized sample; it does not decide the gate)")
    lines.append(g["verdict"])
    lines.append("Rules fixed before measuring:")
    lines += [f"  - {k}: {v}" for k, v in g["rules"].items()]
    lines.append("Proposed real-phase limits (not enforced anywhere, no trading code exists here):")
    lines += [f"  - {k}: {v}" for k, v in g["proposed_risk_limits"].items()]
    return "\n".join(lines)


def to_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=1, default=str, ensure_ascii=False)
