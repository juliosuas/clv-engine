from datetime import datetime, timezone

import pytest

from clv_engine.arb import Leg, best_size, cross_venue, multi_outcome
from clv_engine.fees import FeeModel
from clv_engine.models import MatchedGame, OutcomeQuote, VenueGame

T = datetime(2026, 10, 21, tzinfo=timezone.utc)
PM, KS = FeeModel("polymarket", rate=0.05), FeeModel("kalshi")


def vg(venue, asks, fees, no=None):
    outs = {k: OutcomeQuote(k, k, f"{venue}-{k}", yes_asks=a, no_asks=(no or {}).get(k, [])) for k, a in asks.items()}
    return VenueGame(venue, "ligamx", venue, "A vs B", ["a", "b"], ["A", "B"], T, outs, fees, True)


def game(*venues):
    g = MatchedGame("ligamx", "ligamx:20261021:a|b", ["a", "b"], {"a": "A", "b": "B"}, T, True)
    for v in venues:
        g.venues[v.venue] = v
    return g


def test_multi_outcome_arb_depth_limited_after_fees():
    v = vg("polymarket", {"a": [(0.30, 100), (0.40, 1000)], "b": [(0.30, 100)], "draw": [(0.30, 50), (0.35, 1000)]}, PM)
    a = multi_outcome(game(v), v, 2000)
    assert a is not None
    # beyond 50 shares the draw leg costs 0.35 -> sum 0.95, still +; beyond 100 'b' has no depth
    assert a.shares == pytest.approx(100)
    assert a.profit == pytest.approx(100 - a.cost)
    assert a.cost == pytest.approx(30 + 30 + 50 * 0.30 + 50 * 0.35 + a.fees)
    assert a.profit > 0


def test_no_arb_when_sum_at_least_one():
    v = vg("polymarket", {"a": [(0.5, 100)], "b": [(0.3, 100)], "draw": [(0.2, 100)]}, PM)
    assert multi_outcome(game(v), v, 2000) is None


def test_fees_can_kill_a_raw_arb():
    # raw sum 0.99 but taker fees ~0.035/share -> negative
    v = vg("polymarket", {"a": [(0.33, 100)], "b": [(0.33, 100)], "draw": [(0.33, 100)]}, PM)
    assert multi_outcome(game(v), v, 2000) is None


def test_cross_venue_yes_no():
    pm = vg("polymarket", {"a": [(0.40, 500)], "b": [(0.45, 500)], "draw": [(0.30, 500)]}, PM)
    ks = vg("kalshi", {"a": [(0.55, 500)], "b": [(0.40, 500)], "draw": [(0.25, 500)]}, KS, no={"a": [(0.52, 500)]})
    arbs = cross_venue(game(pm, ks), 2000)
    kinds = {(tuple((l["venue"], l["side"], l["outcome"]) for l in a.legs)) for a in arbs}
    assert (("polymarket", "yes", "a"), ("kalshi", "no", "a")) in kinds
    best = arbs[0]
    assert best.profit > 0 and best.roi > 0


def test_best_size_requires_depth_on_every_leg():
    assert best_size([Leg("x", "a", "yes", [(0.4, 10)], PM, "r"), Leg("x", "b", "yes", [], PM, "r")], 100) is None
