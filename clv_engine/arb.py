"""Depth-limited, fee-aware arbitrage scanner (detection only; nothing is traded).

A combination of legs is an arb when exactly one leg pays $1/share in every
result and buying q shares of every leg costs less than q after taker fees.

* multi_outcome: all mutually exclusive outcomes on one venue (1X2 or both teams).
  Inside a single Polymarket binary, YES+NO trade on one complementary book, so
  "YES+NO < $1" there is essentially impossible; the realistic in-venue case is
  the sum of YES asks across the 3 soccer outcomes.
* cross_venue: (a) each outcome bought on whichever venue, mixed across venues;
  (b) YES on outcome X at venue A + NO on X at venue B.
Settlement-rule mismatches (90' vs extra time, postponement windows) are a real
risk across venues; alerts say "check rules".
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .fees import FeeModel
from .models import MatchedGame, VenueGame
from .orderbook import Level, fill_for_shares


@dataclass
class Leg:
    venue: str
    outcome: str
    side: str
    asks: list[Level]
    fees: FeeModel
    ref: str


@dataclass
class Arb:
    kind: str
    game_key: str
    league: str
    shares: float
    cost: float
    fees: float
    legs: list[dict] = field(default_factory=list)

    @property
    def profit(self) -> float:
        return self.shares - self.cost

    @property
    def roi(self) -> float:
        return self.profit / self.cost if self.cost else 0.0


def best_size(legs: list[Leg], max_shares: float) -> tuple[float, float, float, list[dict]] | None:
    """Try every depth breakpoint up to max_shares; return the most profitable size."""
    if not legs or any(not l.asks for l in legs):
        return None
    # quick reject on top of book
    if sum(l.asks[0][0] for l in legs) >= 1.0:
        return None
    cands = set()
    for l in legs:
        cum = 0.0
        for _, s in l.asks:
            cum += s
            if cum > max_shares:
                break
            cands.add(round(cum, 6))
    cands.add(max_shares)
    best = None
    for q in sorted(cands):
        if q <= 0:
            continue
        fills = [fill_for_shares(l.asks, q, l.fees) for l in legs]
        if not all(f.complete for f in fills):
            continue
        cost = sum(f.cost for f in fills)
        profit = q - cost
        if best is None or profit > best[0]:
            detail = [{"venue": l.venue, "outcome": l.outcome, "side": l.side, "ref": l.ref, "avg_price": round(f.avg_price, 6),
                       "fee": round(f.fee, 6), "worst_price": f.levels[-1][0] if f.levels else None} for l, f in zip(legs, fills)]
            best = (profit, q, cost, sum(f.fee for f in fills), detail)
    if best is None or best[0] <= 0:
        return None
    _, q, cost, fees, detail = best
    return q, cost, fees, detail


def multi_outcome(game: MatchedGame, vg: VenueGame, max_shares: float) -> Arb | None:
    if not vg.exhaustive:
        return None
    legs = [Leg(vg.venue, k, "yes", o.yes_asks, vg.fees, o.ref) for k, o in vg.outcomes.items()]
    res = best_size(legs, max_shares)
    if not res:
        return None
    q, cost, fees, detail = res
    return Arb("multi_outcome", game.key, game.league, q, cost, fees, detail)


def cross_venue(game: MatchedGame, max_shares: float) -> list[Arb]:
    venues = [v for v in game.venues.values()]
    if len(venues) < 2:
        return []
    keys = set.intersection(*(set(v.outcomes) for v in venues))
    need = len(game.teams) + (1 if game.three_way else 0)
    found: list[Arb] = []
    # (a) mixed YES legs covering every outcome
    if len(keys) == need:
        ordered = sorted(keys)
        for combo in itertools.product(venues, repeat=len(ordered)):
            if len({v.venue for v in combo}) < 2:
                continue
            legs = [Leg(v.venue, k, "yes", v.outcomes[k].yes_asks, v.fees, v.outcomes[k].ref) for k, v in zip(ordered, combo)]
            res = best_size(legs, max_shares)
            if res:
                q, cost, fees, detail = res
                found.append(Arb("cross_venue", game.key, game.league, q, cost, fees, detail))
    # (b) YES X on A + NO X on B
    for k in keys:
        for a, b in itertools.permutations(venues, 2):
            qa, qb = a.outcomes[k], b.outcomes[k]
            no_asks = qb.no_asks
            if not no_asks and not game.three_way and len(b.outcomes) == 2:
                other = next(o for kk, o in b.outcomes.items() if kk != k)  # NBA: NO on X == YES on the other team
                no_asks = other.yes_asks
            if not no_asks:
                continue
            legs = [Leg(a.venue, k, "yes", qa.yes_asks, a.fees, qa.ref), Leg(b.venue, k, "no", no_asks, b.fees, qb.no_ref or qb.ref)]
            res = best_size(legs, max_shares)
            if res:
                q, cost, fees, detail = res
                found.append(Arb("cross_venue", game.key, game.league, q, cost, fees, detail))
    found.sort(key=lambda a: -a.profit)
    return found
