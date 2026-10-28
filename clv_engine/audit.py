"""Why can the declared entry edge be positive while venue CLV is negative? (read-only diagnostic)

For every entry that has a venue closing mid, the two reported numbers are

    edge      = fair_entry / effective_price - 1        (reference vs ALL-IN price, fee included)
    venue_clv = venue_mid_close / avg_price - 1         (own venue's close vs price WITHOUT fee)

so they are not the same quantity and not on the same fee basis. Exactly (multiplicatively):

    (1 + venue_clv) / (1 + edge) = (venue_mid_close / fair_entry) * (effective_price / avg_price)
                                    ^ reference -> close move          ^ taker fee factor

i.e. the gap is "the price moved away from the reference" plus the fee asymmetry. avg_price vs best_ask (book depth)
appears in both numbers and cancels. The entry bid/ask spread (ask vs the mid the reference is made of) is inside
`fair/ask - 1` and cannot be separated without the entry-time bid, which is not stored on the entry (it is in `quotes`).

Nothing here changes the ledger. Works on ledger rows or on a JSON dump (list of row dicts).
"""

from __future__ import annotations

import math
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


def decompose(rows: list[Any]) -> dict[str, Any]:
    """Means over entries with a venue closing mid. Percentages are fractions (0.03 = 3%)."""
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
        })
    keys = [k for k in comp[0] if k != "game"]
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
    return "\n".join([
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
    ])
