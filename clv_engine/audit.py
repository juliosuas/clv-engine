"""Why can the declared entry edge be positive while venue CLV is negative? (read-only diagnostic)

For every entry that has a venue closing mid, the two reported numbers are

    edge      = fair_entry / effective_price - 1        (reference vs ALL-IN price, fee included)
    venue_clv = venue_mid_close / avg_price - 1         (own venue's close vs price WITHOUT fee)

so they are not the same quantity and not on the same fee basis. Exactly (multiplicatively):

    (1 + venue_clv) / (1 + edge) = (venue_mid_close / fair_entry) * (effective_price / avg_price)
                                    ^ reference -> close move          ^ taker fee factor

i.e. the gap is "the price moved away from the reference" plus the fee asymmetry. avg_price vs best_ask (book depth)
appears in both numbers and cancels. The entry bid/ask spread (ask vs the mid the reference is made of) is inside
`fair/ask - 1`. The entry row does not store the entry-time bid, but `quotes` does (about one tick every `snapshot_min_seconds`),
so `attach_entry_book` takes the quote tick nearest to `created_at` and splits, exactly,

    fair / ask = (fair / book_mid) * (book_mid / ask)
                  ^ reference vs own mid   ^ half spread paid by a taker

APPROXIMATION (flagged in the output): the bid/mid come from the nearest stored tick, not from the instant of the fill. The
quotes book is the YES book; for side=no the NO ask is 1 - best_bid(YES) and the NO bid is 1 - best_ask(YES). A tick whose
implied ask differs from the entry's own `best_ask` is NOT used in the headline numbers (the book moved between tick and fill).

Nothing here changes the ledger. Works on ledger rows or on a JSON dump (list of row dicts).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

LOG_NOTE = "logs: ln(1+venue_clv) - ln(1+edge) = ln(close/fair) + ln(effective/avg)"


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _tstat(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return None if sd == 0 else m / (sd / math.sqrt(len(xs)))


def _get(r: Any, k: str) -> Any:
    try:
        return r[k]
    except (KeyError, IndexError):
        return None


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.strptime(str(v), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def attach_entry_book(rows: list[Any], quotes: list[Any], max_gap_minutes: float = 30.0, ask_tol: float = 0.0015) -> list[dict[str, Any]]:
    """Copy of `rows` with the entry-time book estimated from the nearest quote tick (APPROXIMATION, see module doc).

    Adds: bid_entry_approx, mid_entry_approx, spread_entry_approx (all on the entry's side basis), tick_gap_min (signed, tick minus
    created_at), tick_consistent (tick's implied ask equals the entry best_ask within `ask_tol`), book_source ('tick' | None).
    Quotes are matched on (game_key, venue, outcome_key); `quotes` rows need ts, game_key, source, outcome_key, best_bid, best_ask.
    """
    by_key: dict[tuple[str, str, str], list[tuple[datetime, Any]]] = {}
    for q in quotes:
        t = _ts(_get(q, "ts"))
        if t is not None:
            by_key.setdefault((_get(q, "game_key"), _get(q, "source"), _get(q, "outcome_key")), []).append((t, q))
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        row.update(bid_entry_approx=None, mid_entry_approx=None, spread_entry_approx=None, tick_gap_min=None, tick_consistent=None, book_source=None)
        created = _ts(_get(r, "created_at"))
        cands = by_key.get((_get(r, "game_key"), _get(r, "venue"), _get(r, "outcome_key")), [])
        if created is not None and cands:
            t, q = min(cands, key=lambda c: abs((c[0] - created).total_seconds()))
            gap = (t - created).total_seconds() / 60
            yb, ya = _get(q, "best_bid"), _get(q, "best_ask")
            if abs(gap) <= max_gap_minutes and yb is not None and ya is not None:
                if _get(r, "side") == "no":
                    bid, ask = 1.0 - ya, 1.0 - yb
                else:
                    bid, ask = yb, ya
                row.update(tick_gap_min=round(gap, 2), book_source="tick", tick_consistent=abs(ask - _get(r, "best_ask")) <= ask_tol if _get(r, "best_ask") is not None else None)
                if row["tick_consistent"]:
                    row.update(bid_entry_approx=bid, mid_entry_approx=(bid + ask) / 2, spread_entry_approx=ask - bid)
        out.append(row)
    return out


def _spread_block(comp: list[dict[str, Any]]) -> dict[str, Any]:
    """Means of the spread split over the entries that have a consistent entry tick."""
    sp = [c for c in comp if c.get("mid_entry") is not None]
    if not sp:
        return {"n": 0}
    return {
        "n": len(sp),
        "mean_spread_abs": _mean([c["spread_abs"] for c in sp]),
        "ref_vs_mid": _mean([c["ref_vs_mid"] for c in sp]),            # fair / own book mid - 1
        "mid_vs_ask": _mean([c["mid_vs_ask"] for c in sp]),            # own book mid / ask - 1  (= minus the half-spread the taker pays)
        "raw_edge_at_ask": _mean([c["raw_edge_at_ask"] for c in sp]),   # product of the two above, same rows
        "ref_vs_mid_ticks": sum(1 for c in sp if abs(c["fair"] - c["mid_entry"]) <= 0.0051),
        "ref_above_mid": sum(1 for c in sp if c["fair"] > c["mid_entry"] + 1e-9),
        "ref_below_or_at_mid": sum(1 for c in sp if c["fair"] <= c["mid_entry"] + 1e-9),
        "mean_edge_net": _mean([c["edge"] for c in sp]),
        "mean_edge_if_ref_were_mid_net": _mean([c["mid_entry"] / c["eff"] - 1 for c in sp]),  # what the mid alone would "earn" after fee: no information
        "max_gap_min": max(abs(c["tick_gap_min"]) for c in sp),
        "mean_abs_gap_min": _mean([abs(c["tick_gap_min"]) for c in sp]),
    }


def decompose(rows: list[Any], quotes: list[Any] | None = None) -> dict[str, Any]:
    """Means over entries with a venue closing mid. Percentages are fractions (0.03 = 3%).

    With `quotes` (stored book ticks), also separates the entry spread: see attach_entry_book (approximate)."""
    if quotes is not None:
        rows = attach_entry_book(rows, quotes)
    usable = [r for r in rows if _get(r, "venue_mid_close") is not None and _get(r, "avg_price") and _get(r, "fair_entry")]
    n_all = len(rows)
    out: dict[str, Any] = {"entries": n_all, "with_venue_close": len(usable), "games": len({_get(r, "game_key") for r in usable})}
    if not usable:
        return {**out, "note": "no entry has a venue closing mid yet"}
    comp = []
    for r in usable:
        ask, avg, eff, fair, mid = r["best_ask"], r["avg_price"], r["effective_price"], r["fair_entry"], r["venue_mid_close"]
        comp.append({
            "game": r["game_key"],
            "edge": r["edge_entry"],
            "raw_edge_at_ask": fair / ask - 1,        # reference vs best ask, no fee (includes entry half-spread)
            "depth_slippage": avg / ask - 1,          # avg fill vs best ask
            "fee": eff / avg - 1,                     # taker fee as % of avg price
            "ref_to_close_move": mid / fair - 1,      # reference at entry -> own venue close
            "venue_clv_gross": mid / avg - 1,         # what the ledger calls venue_clv_pct
            "venue_clv_net": mid / eff - 1,           # same basis as the edge (fee included)
            "if_price_did_not_move_gross": fair / avg - 1,
            "if_price_did_not_move_net": fair / eff - 1,
            "ln_move": math.log(mid / fair),
            "ln_fee": math.log(eff / avg),
            "ln_gap": math.log(mid / avg) - math.log(fair / eff),
            "fair": fair, "eff": eff, "mid_entry": _get(r, "mid_entry_approx"), "tick_gap_min": _get(r, "tick_gap_min"),
        })
        if comp[-1]["mid_entry"] is not None:
            m_e = comp[-1]["mid_entry"]
            comp[-1].update(spread_abs=_get(r, "spread_entry_approx"), ref_vs_mid=fair / m_e - 1, mid_vs_ask=m_e / ask - 1)
    keys = [k for k in comp[0] if k not in ("game", "fair", "eff", "mid_entry", "tick_gap_min", "spread_abs", "ref_vs_mid", "mid_vs_ask")]
    out["means"] = {k: _mean([c[k] for c in comp]) for k in keys}
    out["t_stat_entries"] = {k: _tstat([c[k] for c in comp]) for k in ("venue_clv_gross", "ref_to_close_move", "edge")}
    by_game: dict[str, list[float]] = {}
    for c in comp:
        by_game.setdefault(c["game"], []).append(c["venue_clv_gross"])
    gm = [sum(v) / len(v) for v in by_game.values()]
    out["game_level_venue_clv_gross"] = {"games": len(gm), "mean": _mean(gm), "t_stat": _tstat(gm)}
    v = sorted(c["venue_clv_gross"] for c in comp)
    out["median_venue_clv_gross"] = v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2
    ticks = sum(1 for r in usable if r["fair_entry"] - r["best_ask"] <= 0.0151)
    out["reference_within_1_5_cents_of_ask"] = ticks
    out["ref_sources"] = sorted({str(_get(r, "ref_source")) for r in usable})
    out["note"] = LOG_NOTE
    if quotes is not None:
        out["entry_spread"] = _spread_block(comp)
        out["entry_spread"]["tick_inconsistent"] = [(_get(r, "id"), _get(r, "venue"), _get(r, "side")) for r in usable if _get(r, "book_source") == "tick" and not _get(r, "tick_consistent")]
        out["entry_spread"]["no_tick"] = [_get(r, "id") for r in usable if _get(r, "book_source") is None]
    return out


def format_decomposition(d: dict[str, Any]) -> str:
    if "means" not in d:
        return f"EDGE vs VENUE-CLV AUDIT: {d['with_venue_close']} of {d['entries']} entries have a venue close; {d.get('note', '')}"
    m, t = d["means"], d["t_stat_entries"]

    def p(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.2%}"

    def ts(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.2f}"

    g = d["game_level_venue_clv_gross"]
    lines = [
        f"EDGE vs VENUE-CLV AUDIT ({d['with_venue_close']} entries with a venue close, {d['games']} games; ref_source={','.join(d['ref_sources'])})",
        f"declared edge (reference / all-in price - 1):              {p(m['edge'])}  (t={ts(t['edge'])})",
        f"  reference vs best ask, before fee (includes half-spread): {p(m['raw_edge_at_ask'])}",
        f"  depth slippage avg_price vs best_ask:                     {p(m['depth_slippage'])}   (in both numbers; cancels in the gap)",
        f"  taker fee on avg price:                                   {p(m['fee'])}   (in the edge, NOT in venue_clv)",
        f"venue_clv as stored (close mid / avg_price - 1, no fee):    {p(m['venue_clv_gross'])}  (t={ts(t['venue_clv_gross'])}, median {p(d['median_venue_clv_gross'])}; per game: {p(g['mean'])}, t={ts(g['t_stat'])}, n={g['games']})",
        f"venue_clv on the edge's basis (close mid / all-in - 1):     {p(m['venue_clv_net'])}",
        f"if prices had NOT moved from the reference: gross {p(m['if_price_did_not_move_gross'])}, net {p(m['if_price_did_not_move_net'])}",
        f"reference at entry -> own venue close (price move):         {p(m['ref_to_close_move'])}  (t={ts(t['ref_to_close_move'])})",
        f"gap in logs {m['ln_gap']:+.3f} = move {m['ln_move']:+.3f} + fee {m['ln_fee']:+.3f}  ({d['note']})",
        f"entries whose reference is within 1.5 cents of the ask (about a tick): {d['reference_within_1_5_cents_of_ask']} of {d['with_venue_close']}",
    ]
    if "entry_spread" in d:
        lines.extend(_format_spread(d))
    return "\n".join(lines)


def _format_spread(d: dict[str, Any]) -> list[str]:
    e = d["entry_spread"]
    head = "ENTRY SPREAD vs MID (APPROXIMATION: bid/mid from the nearest stored quote tick, not the fill instant; YES book, NO = 1 - opposite side)"
    if not e.get("n"):
        return [head, "  no entry has a quote tick whose ask matches its best_ask: spread not separable"]

    def p(x: float | None) -> str:
        return "n/a" if x is None else f"{x:+.2%}"

    return [
        head,
        f"  {e['n']} of {d['with_venue_close']} entries usable (tick ask == entry best_ask); tick gap mean {e['mean_abs_gap_min']:.1f} min, max {e['max_gap_min']:.1f} min",
        f"  mean quoted spread (ask - bid):                          {e['mean_spread_abs']:.4f} (price units)",
        f"  reference vs own book mid (fair / mid - 1):              {p(e['ref_vs_mid'])}   ({e['ref_above_mid']} above mid, {e['ref_below_or_at_mid']} at/below; {e['ref_vs_mid_ticks']} within half a cent of it)",
        f"  own book mid vs ask (mid / ask - 1 = half-spread paid):  {p(e['mid_vs_ask'])}",
        f"  => reference vs ask before fee (same rows, product):      {p(e['raw_edge_at_ask'])}",
        f"  declared edge net of fee, same rows:                     {p(e['mean_edge_net'])}",
        f"  edge if the reference were just the own mid (net of fee): {p(e['mean_edge_if_ref_were_mid_net'])}",
        f"  excluded: tick ask != entry best_ask {e['tick_inconsistent']}; no tick {e['no_tick']}",
    ]
