"""Measured-sample status and the paper->real graduation gate (REPORTING ONLY).

Nothing in here, or anywhere in this package, places orders or touches a wallet: the gate only prints
whether the paper evidence meets a set of *proposed* thresholds. The thresholds are proposals for Julio
to approve (see docs/GRADUATION.md); they are not facts and not recommendations to trade.

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
from datetime import datetime
from statistics import NormalDist
from typing import Any

from .ledger import Ledger
from .util import parse_datetime, utc_now

MIN_RATE_ENTRIES = 10  # fewer measured entries than this: entries/day is not estimated
MIN_RATE_DAYS = 2.0    # observation span shorter than this: entries/day is not estimated
MIN_CI_ENTRIES = 5

# PROPOSED real-phase limits, for Julio to approve or change. Informational only: no code enforces
# or acts on them because the repo has no trading code.
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


def _group_clv(rows: list[Any], key: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in rows:
        out.setdefault(r[key], []).append(r["clv_pct"])
    return out


def _cis(rows: list[Any], settings) -> dict[str, Any]:
    clv = [r["clv_pct"] for r in rows]
    by_game: dict[str, list[float]] = {}
    for r in rows:
        by_game.setdefault(r["game_key"], []).append(r["clv_pct"])
    return {
        "t": t_interval(clv, settings.grad_ci_level),
        "bootstrap": cluster_bootstrap(list(by_game.values()), settings.grad_ci_level, settings.grad_bootstrap_samples),
        "games": len(by_game),
    }


# ---- status -----------------------------------------------------------------
def status(ledger: Ledger, settings, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    allrows = ledger.entries()
    sample = measured_sample(ledger, settings)
    clv = [r["clv_pct"] for r in sample]
    settled = [r for r in sample if r["status"] == "settled"]
    out: dict[str, Any] = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entries_total": len(allrows),
        "entries_with_pinnacle_close": sum(1 for r in allrows if r["sharp_fair_close"] is not None),
        "pending_close": sum(1 for r in allrows if r["clv_status"] == "pending_close"),
        "pending_sharp": sum(1 for r in allrows if r["clv_status"] == "pending_sharp"),
        "settled_total": sum(1 for r in allrows if r["status"] == "settled"),
        "sample_n": len(sample),
        "sample_settled": len(settled),
        "mean_clv": mean(clv) if clv else None,
        "beat_close": (sum(1 for c in clv if c > 0) / len(clv)) if clv else None,
        "ci_level": settings.grad_ci_level,
        "ci": _cis(sample, settings) if sample else {"t": None, "bootstrap": None, "games": 0},
        "target": settings.grad_min_entries,
        "entries_per_day": None,
        "days_to_target": None,
        "projection": "",
    }
    first = min((parse_datetime(r["created_at"]) for r in sample), default=None)
    span = (now - first).total_seconds() / 86400 if first else 0.0
    out["observed_days"] = span
    if len(sample) >= settings.grad_min_entries:
        out["entries_per_day"] = len(sample) / span if span > 0 else None
        out["days_to_target"] = 0.0
        out["projection"] = f"target of {settings.grad_min_entries} measured entries reached"
    elif len(sample) < MIN_RATE_ENTRIES or span < MIN_RATE_DAYS:
        out["projection"] = (f"insufficient data: need >= {MIN_RATE_ENTRIES} measured entries over >= {MIN_RATE_DAYS:g} days to estimate a rate "
                             f"(have {len(sample)} over {span:.1f} days)")
    else:
        rate = len(sample) / span
        out["entries_per_day"] = rate
        out["days_to_target"] = (settings.grad_min_entries - len(sample)) / rate
        out["projection"] = f"~{out['days_to_target']:.0f} days to {settings.grad_min_entries} at the observed rate (naive: assumes the rate holds)"
    usage = ledger.usage(now.strftime("%Y-%m-%d"))
    out["quota_today"] = {"credits": usage["credits"], "requests": usage["requests"], "skipped": usage["skipped"],
                          "remaining": usage["remaining"], "budget": settings.odds_api_daily_budget}
    return out


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def _ci_txt(ci: tuple[float, float] | None) -> str:
    return "n/a (too few entries)" if ci is None else f"[{ci[0]:+.2%}, {ci[1]:+.2%}]"


def format_status(st: dict[str, Any]) -> str:
    q = st["quota_today"]
    lvl = f"{st['ci_level']:.0%}"
    beat = "n/a" if st["beat_close"] is None else f"{st['beat_close']:.1%}"
    lines = [
        f"CLV STATUS (paper only, as of {st['as_of']})",
        f"entries total: {st['entries_total']} | with captured Pinnacle close: {st['entries_with_pinnacle_close']} | "
        f"pending close: {st['pending_close']} | pending (no Pinnacle close): {st['pending_sharp']} | settled: {st['settled_total']}",
        f"measured sample (passes current entry rules, Pinnacle ref + close, full fill): {st['sample_n']} entries in {st['ci']['games']} games, "
        f"{st['sample_settled']} settled; target {st['target']}",
        f"mean CLV after fees: {_pct(st['mean_clv'])} | beat the close: {beat}",
        f"{lvl} CI, game-clustered bootstrap: {_ci_txt(st['ci']['bootstrap'])} | {lvl} CI, t (entries independent, optimistic): {_ci_txt(st['ci']['t'])}",
        "entries/day (measured sample): " + ("n/a" if st["entries_per_day"] is None else f"{st['entries_per_day']:.2f}") + f" over {st['observed_days']:.1f} days",
        f"projection: {st['projection']}",
        f"Odds API credits today (local count, UTC day): {q['credits']:.0f}/{q['budget']} in {q['requests']} calls, {q['skipped']} polls skipped by budget; "
        f"last x-requests-remaining: {'n/a' if q['remaining'] is None else q['remaining']}",
    ]
    return "\n".join(lines)


# ---- graduation gate -----------------------------------------------------------
def graduation(ledger: Ledger, settings, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    sample = measured_sample(ledger, settings)
    n = len(sample)
    crit: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        crit.append({"name": name, "ok": ok, "detail": detail})

    add("sample size", n >= settings.grad_min_entries, f"{n} measured entries (need >= {settings.grad_min_entries})")
    clv = [r["clv_pct"] for r in sample]
    m = mean(clv) if clv else None
    add("mean CLV > 0", None if m is None else m > 0, f"mean CLV {_pct(m)}")
    cis = _cis(sample, settings) if sample else {"t": None, "bootstrap": None, "games": 0}
    lowers = [ci[0] for ci in (cis["bootstrap"], cis["t"]) if ci]
    lvl = f"{settings.grad_ci_level:.0%}"
    add(f"{lvl} CI lower bound > 0 (both methods)", None if len(lowers) < 2 else min(lowers) > 0,
        f"bootstrap (game-clustered) {_ci_txt(cis['bootstrap'])}; t {_ci_txt(cis['t'])}")
    settled = [r for r in sample if r["status"] == "settled"]
    pnl = sum(r["pnl"] or 0 for r in settled)
    cost = sum(r["stake"] + r["fee"] for r in settled)
    enough_settled = n > 0 and len(settled) >= settings.grad_min_settled_fraction * n
    add("after-fee paper P&L > 0 on the same sample", None if not enough_settled else pnl > 0,
        f"P&L ${pnl:,.2f} on ${cost:,.2f} staked ({len(settled)}/{n} settled; need >= {settings.grad_min_settled_fraction:.0%} settled)")
    by_league = _group_clv(sample, "league")
    big = {k: v for k, v in by_league.items() if len(v) >= settings.grad_min_group_entries}
    top_share = (max(len(v) for v in by_league.values()) / n) if n else None
    rest_ok = True
    for lg in by_league:
        rest = [c for k, v in by_league.items() if k != lg for c in v]
        if len(rest) < settings.grad_min_group_entries or mean(rest) <= 0:
            rest_ok = False
    league_ok = bool(n) and len(big) >= settings.grad_min_leagues and top_share <= settings.grad_max_league_share and rest_ok \
        and all(mean(v) > 0 for v in big.values())
    add("no single league drives the result", None if not n else league_ok,
        f"leagues with >= {settings.grad_min_group_entries} entries: {len(big)} (need >= {settings.grad_min_leagues}); "
        f"top league share {'n/a' if top_share is None else f'{top_share:.0%}'} (max {settings.grad_max_league_share:.0%}); "
        f"mean CLV by league: " + (", ".join(f"{k} {_pct(mean(v))} (n={len(v)})" for k, v in sorted(by_league.items())) or "n/a")
        + "; leave-one-league-out mean CLV > 0 required")
    by_venue = _group_clv(sample, "venue")
    bigv = {k: v for k, v in by_venue.items() if len(v) >= settings.grad_min_group_entries}
    restv_ok = True
    if len(by_venue) > 1:
        for vn in by_venue:
            rest = [c for k, v in by_venue.items() if k != vn for c in v]
            if len(rest) < settings.grad_min_group_entries or mean(rest) <= 0:
                restv_ok = False
    venue_ok = bool(n) and len(bigv) >= settings.grad_min_venues and all(mean(v) > 0 for v in bigv.values()) and restv_ok
    add("no single venue drives the result", None if not n else venue_ok,
        f"venues with >= {settings.grad_min_group_entries} entries: {len(bigv)} (need >= {settings.grad_min_venues}); "
        f"mean CLV by venue: " + (", ".join(f"{k} {_pct(mean(v))} (n={len(v)})" for k, v in sorted(by_venue.items())) or "n/a")
        + f"; excluded venues now: {','.join(settings.excluded_venues) or '-'}")

    met = sum(1 for c in crit if c["ok"] is True)
    passed = met == len(crit)
    return {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": passed,
        "met": met,
        "total": len(crit),
        "criteria": crit,
        "sample_n": n,
        "proposed_risk_limits": PROPOSED_RISK_LIMITS,
        "verdict": ("ALL CRITERIA MET on paper. This is evidence for a decision by Julio, not an authorization: nothing in this repo places orders, "
                    "and any real-money step needs his explicit approval plus separately reviewed code."
                    if passed else f"NOT READY: {met}/{len(crit)} criteria met (unknown/insufficient counts as not met)."),
    }


def format_graduation(g: dict[str, Any]) -> str:
    mark = {True: "PASS", False: "FAIL", None: "N/A "}
    lines = [f"GRADUATION CHECK (paper -> real; proposed thresholds, for Julio to approve; as of {g['as_of']})"]
    lines += [f"[{mark[c['ok']]}] {c['name']}: {c['detail']}" for c in g["criteria"]]
    lines.append(g["verdict"])
    lines.append("Proposed real-phase limits (not enforced anywhere, no trading code exists here):")
    lines += [f"  - {k}: {v}" for k, v in g["proposed_risk_limits"].items()]
    return "\n".join(lines)


def to_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=1, default=str, ensure_ascii=False)
