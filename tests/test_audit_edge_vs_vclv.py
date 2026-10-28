"""Why +edge but -venue CLV: SYNTHETIC reproductions (no real data in this file) of the mechanisms found in the audit.

Real-dump findings are in the report; the dump itself is not committed.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from clv_engine.alerts import AlertManager
from clv_engine.audit import decompose, format_decomposition
from clv_engine.config import Settings
from clv_engine.engine import CycleStats, Engine
from clv_engine.fees import FeeModel
from clv_engine.ledger import Ledger
from clv_engine.models import MatchedGame, OutcomeQuote, VenueGame
from clv_engine.sharp.base import NoSharpSource
from clv_engine.strategy import consensus_fair, evaluate, venue_fair

START = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
NOW = START - timedelta(hours=5)


def venue(name, a_bid, a_ask, b_bid, b_ask, rate=0.05):
    """Two-way market; NO asks derived from the opposite YES bids like the real adapters."""
    a = OutcomeQuote("a", "A", f"{name}-a", yes_asks=[(a_ask, 1e5)], yes_bids=[(a_bid, 1e5)], no_asks=[(round(1 - b_bid, 4), 1e5)])
    b = OutcomeQuote("b", "B", f"{name}-b", yes_asks=[(b_ask, 1e5)], yes_bids=[(b_bid, 1e5)], no_asks=[(round(1 - a_bid, 4), 1e5)])
    return VenueGame(name, "nba", "ev", "A vs B", ["a", "b"], ["A", "B"], START, {"a": a, "b": b}, FeeModel(name, rate=rate), False)


def game_of(*vgs):
    return MatchedGame("nba", "g1", ["a", "b"], {"a": "A", "b": "B"}, START, False, {v.venue: v for v in vgs}, None)


def settings(**kw):
    s = Settings(db_path=":memory:", leagues=["nba"], alert_sinks=[], excluded_leagues=[], excluded_venues=[], min_depth_usd=0, require_pinnacle_ref=False,
                 min_edge=0.02, entry_sides=["yes", "no"], stake_usd=100.0, snapshot_min_seconds=0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def engine(s):
    return Engine(s, Ledger(":memory:"), None, None, NoSharpSource("test"), AlertManager([], Ledger(":memory:"), 1800))


# ---- 1. the consensus reference is the other venue's mid: an ask a tick below it is an "edge" -------------------------------
def test_consensus_edge_is_just_other_venue_mid_vs_own_ask():
    pm = venue("polymarket", 0.395, 0.40, 0.58, 0.60, rate=0.0)  # own ask for A = 0.40
    ks = venue("kalshi", 0.40, 0.42, 0.57, 0.59)       # other venue: mid A = 0.41
    g = game_of(pm, ks)
    ref = consensus_fair(g, exclude_venue="polymarket")
    assert ref["a"] == pytest.approx(venue_fair(ks)["a"])  # the reference IS kalshi's normalized mid, no outside information
    (c,) = [c for c in evaluate(g, pm, settings(entry_reference="consensus", min_edge=0.02, entry_sides=["yes"]), NOW) if c.outcome.key == "a"]
    assert c.ref_source == "consensus" and c.edge > 0.02
    assert c.fair - c.fill.avg_price < 0.02  # about one cent: inside the tick/spread, not information
    fee_pm = venue("polymarket", 0.395, 0.40, 0.58, 0.60, rate=0.05)  # the same quote with the real taker fee: the "edge" is gone
    assert [c for c in evaluate(game_of(fee_pm, ks), fee_pm, settings(entry_reference="consensus", min_edge=0.02, entry_sides=["yes"]), NOW)
            if c.outcome.key == "a"] == []


def test_synthetic_gap_when_prices_do_not_move_vs_when_they_do():
    # Entry on polymarket ask 0.40 with fee; reference 0.41. If the close equals the reference the venue CLV is >= the edge...
    base = dict(game_key="g", best_ask=0.40, avg_price=0.40, effective_price=0.40 * 1.03, fair_entry=0.43, edge_entry=0.43 / (0.40 * 1.03) - 1,
                ref_source="consensus")
    still = {**base, "venue_mid_close": 0.43}
    moved = {**base, "game_key": "g2", "venue_mid_close": 0.35}  # ...and a 6-cent drop makes it hugely negative while the edge stays "+"
    d = decompose([still])
    assert d["means"]["venue_clv_gross"] == pytest.approx(0.43 / 0.40 - 1) and d["means"]["venue_clv_gross"] > d["means"]["edge"]
    d2 = decompose([moved])
    assert d2["means"]["edge"] > 0 > d2["means"]["venue_clv_gross"]
    # exact identity: ln(1+vclv) - ln(1+edge) = ln(close/fair) + ln(eff/avg)
    m = d2["means"]
    assert m["ln_gap"] == pytest.approx(m["ln_move"] + m["ln_fee"], abs=1e-12)
    assert m["ln_gap"] == pytest.approx(math.log(1 + m["venue_clv_gross"]) - math.log(1 + m["edge"]), abs=1e-12)


def test_fee_basis_mismatch_edge_is_net_venue_clv_is_gross():
    row = dict(game_key="g", best_ask=0.50, avg_price=0.50, effective_price=0.5125, fair_entry=0.53, edge_entry=0.53 / 0.5125 - 1, venue_mid_close=0.50,
               ref_source="consensus")
    m = decompose([row])["means"]
    assert m["venue_clv_gross"] == pytest.approx(0.0)           # price did not move from the entry price: "0%"
    assert m["venue_clv_net"] == pytest.approx(0.50 / 0.5125 - 1)  # on the edge's basis it is a loss equal to the fee
    assert m["venue_clv_net"] < 0
    assert m["fee"] == pytest.approx(0.025)


def test_audit_formats_and_handles_no_closes():
    assert "no entry has a venue closing mid" in format_decomposition(decompose([]))
    row = dict(game_key="g", best_ask=0.5, avg_price=0.5, effective_price=0.51, fair_entry=0.53, edge_entry=0.039, venue_mid_close=0.45, ref_source="consensus")
    txt = format_decomposition(decompose([row, {**row, "game_key": "g2"}, {**row, "game_key": "g3"}]))
    assert "price move" in txt and "NOT in venue_clv" in txt and "consensus" in txt


# ---- 2. one-sided books: the "mid" must not silently be the ask ---------------------------------------------------------------
def test_one_sided_book_has_no_mid_for_references_or_closes():
    q = OutcomeQuote("a", "A", "t", yes_asks=[(0.60, 100)], yes_bids=[])
    assert q.mid == 0.60 and q.mid2 is None  # legacy `mid` is just the ask
    thin = venue("kalshi", 0.30, 0.60, 0.38, 0.70)
    thin.outcomes["a"].yes_bids = []  # one side empty
    assert venue_fair(thin) is None
    eng = engine(settings(entry_sides=["yes"]))
    g = game_of(thin)
    g.sharp = None
    eng.process_game(g, NOW, CycleStats())
    assert eng.ledger.closing("g1", "a", "kalshi") is None  # nothing stored instead of the ask posing as a close
    assert eng.ledger.closing("g1", "b", "kalshi") is not None


# ---- 3. duplicate / hedged entries -----------------------------------------------------------------------------------------
def kalshi_two_way(a_ask, b_ask):
    """Kalshi lists a market per team: YES A and NO B are the same bet and both can be 'cheap'."""
    return venue("kalshi", a_ask - 0.01, a_ask, b_ask - 0.01, b_ask, rate=0.0)


def test_equivalent_bets_in_a_two_way_market_are_one_entry():
    eng = engine(settings(entry_reference="consensus", min_edge=-1.0, max_entries_per_outcome=1))
    eng.s.entry_sides = ["yes", "no"]
    ks, pm = kalshi_two_way(0.48, 0.52), venue("polymarket", 0.50, 0.51, 0.49, 0.50)
    g = game_of(pm, ks)
    eng.paper_entries(g, NOW, CycleStats())
    rows = [r for r in eng.ledger.entries() if r["venue"] == "kalshi"]
    bets = {(r["outcome_key"], r["side"]) for r in rows}
    assert len(rows) <= 1 and not ({("a", "yes"), ("b", "no")} <= bets)  # never both YES A and NO B on the same venue
    eng.paper_entries(g, NOW, CycleStats())  # next cycle: still no second copy
    assert len([r for r in eng.ledger.entries() if r["venue"] == "kalshi"]) == len(rows)


def test_opposite_entries_on_two_venues_are_not_opened_unless_allowed():
    s = settings(entry_reference="consensus", min_edge=-1.0, entry_sides=["yes", "no"])
    eng = engine(s)
    eng.ledger.add_entry(venue="polymarket", league="nba", game_key="g1", outcome_key="a", outcome_label="A", side="no", stake=100, shares=200,
                         avg_price=0.5, fee=0, effective_price=0.5, ref_source="consensus", fair_entry=0.5, edge_entry=0.0)
    assert eng._duplicate_or_hedge(game_of(), "kalshi", "a", "yes") == "hedge"  # YES A on kalshi would hedge NO A on polymarket
    assert eng._duplicate_or_hedge(game_of(), "kalshi", "a", "no") == ""  # same side on another venue is not a hedge
    eng.s.allow_hedged_entries = True
    assert eng._duplicate_or_hedge(game_of(), "kalshi", "a", "yes") == ""


# ---- 4. paper fills are always taker -------------------------------------------------------------------------------------
def test_paper_fills_are_taker_only():
    pm = venue("polymarket", 0.395, 0.40, 0.58, 0.60, rate=0.05)
    g = game_of(pm)
    for c in evaluate(g, pm, settings(entry_reference="consensus", min_edge=-1.0, entry_sides=["yes", "no"]), NOW):
        book = c.outcome.yes_asks if c.side == "yes" else c.outcome.no_asks
        assert c.fill.avg_price >= book[0][0] - 1e-9          # walks the asks, never rests at the bid
        assert c.fill.fee > 0 and c.fill.effective_price > c.fill.avg_price  # taker fee always charged
        assert "taker" in pm.fees.describe()
    eng = engine(settings(entry_reference="consensus", min_edge=-1.0))
    eng.paper_entries(game_of(pm, venue("kalshi", 0.40, 0.42, 0.57, 0.59)), NOW, CycleStats())
    assert eng.ledger.entries() and all("taker" in r["fee_model"] for r in eng.ledger.entries())


def test_audit_cli_reads_a_json_dump(tmp_path, capsys):
    import json

    from clv_engine.cli import main

    row = dict(game_key="g", best_ask=0.5, avg_price=0.5, effective_price=0.51, fair_entry=0.53, edge_entry=0.039, venue_mid_close=0.45, ref_source="consensus")
    f = tmp_path / "dump.json"
    f.write_text(json.dumps([row, {**row, "game_key": "g2"}, {**row, "game_key": "g3"}]))
    assert main(["--env-file", "", "audit", "--dump", str(f)]) == 0
    assert "EDGE vs VENUE-CLV AUDIT" in capsys.readouterr().out
