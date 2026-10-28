"""Harder entry filters (MIN_EDGE after fees, depth, full fill, exclusions) and NFL support. No network."""

from datetime import datetime, timedelta, timezone

import pytest

from clv_engine.alerts import AlertManager
from clv_engine.config import Settings
from clv_engine.engine import Engine
from clv_engine.fees import FeeModel
from clv_engine.leagues import DEFAULT_ORDER, LEAGUES, get_leagues
from clv_engine.ledger import Ledger
from clv_engine.matching import match_games
from clv_engine.models import BookLine, MatchedGame, OutcomeQuote, SharpGame, VenueGame
from clv_engine.sharp import make_sharp_source
from clv_engine.strategy import ask_depth_usd, evaluate
from clv_engine.teams import canonical
from clv_engine.venues.kalshi import KalshiAdapter
from clv_engine.venues.polymarket import PolymarketAdapter

START = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
NOW = START - timedelta(hours=5)


def make_game(asks, venue="polymarket", fair_a=0.60, league="nba", rate=0.05):
    """Two-way game; outcome 'a' has YES asks `asks`, 'b' is priced so it never qualifies."""
    a = OutcomeQuote("a", "A", "tok-a", yes_asks=asks, no_asks=[(0.99, 100000)])
    b = OutcomeQuote("b", "B", "tok-b", yes_asks=[(0.60, 100000)], no_asks=[(0.99, 100000)])
    vg = VenueGame(venue, league, "ev", "A vs B", ["a", "b"], ["A", "B"], START, {"a": a, "b": b}, FeeModel(venue, rate=rate), False)
    line = BookLine("pinnacle", {"a": 1 / fair_a, "b": 1 / (1 - fair_a)}, {"a": fair_a, "b": 1 - fair_a})
    sg = SharpGame(league, "s1", "a", "b", "A", "B", START, line)
    return MatchedGame(league, "g", ["a", "b"], {"a": "A", "b": "B"}, START, False, {venue: vg}, sg), vg


def cfg(**kw):
    base = dict(entry_sides=["yes"], excluded_leagues=[], excluded_venues=[], min_depth_usd=0.0, stake_usd=100.0)
    base.update(kw)
    return Settings(**base)


def test_defaults_are_the_hard_filters():
    s = Settings()
    assert s.min_edge >= 0.05 and s.require_pinnacle_ref and s.min_depth_usd > s.stake_usd
    assert s.excluded_leagues == ["ucl"] and s.excluded_venues == ["kalshi"]
    assert s.allow_partial is False  # full fill required by default


def test_min_edge_is_after_fees_vs_pinnacle():
    # ask 0.55 vs fair 0.60: raw edge 9.1%; fee 0.05*0.55*0.45=1.2375c/share -> effective 0.5624 -> edge 6.7%
    game, vg = make_game([(0.55, 100000)])
    (c,) = [c for c in evaluate(game, vg, cfg(min_edge=0.05), NOW) if c.outcome.key == "a"]
    assert c.fill.effective_price == pytest.approx(0.55 + 0.05 * 0.55 * 0.45, abs=1e-6)
    assert c.edge == pytest.approx(0.60 / c.fill.effective_price - 1, abs=1e-9)
    assert c.edge < 0.60 / 0.55 - 1  # fees reduce the edge
    # the same quote fails a 7% bar only because the fee is counted
    assert [c for c in evaluate(game, vg, cfg(min_edge=0.07), NOW) if c.outcome.key == "a"] == []
    # a 5.5%-raw-edge ask is below the 5% bar after fees
    game2, vg2 = make_game([(0.568, 100000)])  # raw 5.6%, after fee < 5%
    assert [c for c in evaluate(game2, vg2, cfg(min_edge=0.05), NOW) if c.outcome.key == "a"] == []


def test_pinnacle_required_even_with_consensus_available():
    game, vg = make_game([(0.50, 100000)])
    game.sharp = None
    other = VenueGame("kalshi", "nba", "k", "", ["a", "b"], ["A", "B"], START,
                      {"a": OutcomeQuote("a", "A", "x", yes_asks=[(0.61, 1000)], yes_bids=[(0.60, 1000)]),
                       "b": OutcomeQuote("b", "B", "y", yes_asks=[(0.41, 1000)], yes_bids=[(0.39, 1000)])},
                      FeeModel("kalshi"), False)
    game.venues["kalshi"] = other
    assert evaluate(game, vg, cfg(entry_reference="auto", require_pinnacle_ref=True), NOW) == []
    assert evaluate(game, vg, cfg(entry_reference="auto", require_pinnacle_ref=False), NOW) != []


def test_ask_depth_counts_only_levels_near_the_quote():
    asks = [(0.50, 100), (0.505, 100), (0.60, 10_000)]
    assert ask_depth_usd(asks, 0.01) == pytest.approx(0.50 * 100 + 0.505 * 100)
    assert ask_depth_usd([], 0.01) == 0.0
    assert ask_depth_usd(list(reversed(asks)), 0.0) == pytest.approx(50.0)


def test_min_depth_blocks_thin_books_even_if_stake_fills():
    # $100 stake fills at 0.50 (needs 200 shares); only ~$150 rests within 1c, deep liquidity far above
    thin = [(0.50, 300), (0.80, 100000)]
    game, vg = make_game(thin, fair_a=0.70)
    assert [c for c in evaluate(game, vg, cfg(min_depth_usd=0), NOW) if c.outcome.key == "a"]
    assert [c for c in evaluate(game, vg, cfg(min_depth_usd=250), NOW) if c.outcome.key == "a"] == []
    deep = [(0.50, 1000), (0.80, 100000)]
    game, vg = make_game(deep, fair_a=0.70)
    assert [c for c in evaluate(game, vg, cfg(min_depth_usd=250), NOW) if c.outcome.key == "a"]


def test_full_fill_required_unless_partial_allowed():
    # only 100 shares (=$50) rest in the book: the $100 stake cannot fill
    game, vg = make_game([(0.50, 100)], fair_a=0.70)
    assert [c for c in evaluate(game, vg, cfg(), NOW) if c.outcome.key == "a"] == []
    got = [c for c in evaluate(game, vg, cfg(allow_partial=True), NOW) if c.outcome.key == "a"]
    assert got and not got[0].fill.complete


@pytest.mark.parametrize("league,venue,kw", [("ucl", "polymarket", dict(excluded_leagues=["ucl"])), ("nba", "kalshi", dict(excluded_venues=["kalshi"]))])
def test_excluded_league_or_venue_creates_no_entries(league, venue, kw):
    game, vg = make_game([(0.40, 100000)], venue=venue, league=league)
    assert [c for c in evaluate(game, vg, cfg(), NOW) if c.outcome.key == "a"], "control: allowed without the exclusion"
    assert evaluate(game, vg, cfg(**kw), NOW) == []


# ---- engine-level: exclusions keep existing data, polling skips excluded leagues -----------------------------------------
def engine(http, ledger=None, **kw):
    s = Settings(db_path=":memory:", leagues=["nba", "nfl", "ucl"], alert_sinks=[], snapshot_min_seconds=0, sharp_source="theoddsapi",
                 odds_api_key="test-key", min_depth_usd=0, **kw)
    ledger = ledger or Ledger(":memory:")
    return Engine(s, ledger, PolymarketAdapter(http), KalshiAdapter(http), make_sharp_source(s, http, ledger), AlertManager([], ledger, 1800))


def test_excluded_league_without_pending_entries_is_not_polled(http):
    eng = engine(http, excluded_leagues=["ucl"])
    eng.cycle(datetime(2026, 10, 19, tzinfo=timezone.utc))
    polled = {q.get("series_id") for u, q in http.calls if "gamma-api" in u and u.endswith("/events")}
    assert "10204" not in polled  # UCL series never requested
    assert {"10345", "12185"} <= polled


def test_excluded_league_with_pending_entry_is_still_polled_for_its_close(http):
    led = Ledger(":memory:")
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    led.add_entry(venue="polymarket", league="ucl", game_key="g1", outcome_key="a", outcome_label="A", side="yes",
                  start_time="2026-10-19T20:00:00Z", stake=100, shares=200, avg_price=0.5, fee=1, effective_price=0.505,
                  ref_source="pinnacle", fair_entry=0.6, edge_entry=0.1)
    eng = engine(http, led, excluded_leagues=["ucl"])
    assert eng.league_active(LEAGUES["ucl"], now)
    assert not eng.league_active(LEAGUES["ucl"], now + timedelta(days=1))  # kickoff passed: nothing left to wait for
    eng.cycle(now)
    assert any(q.get("series_id") == "10204" for u, q in http.calls if "gamma-api" in u)
    assert len(led.entries("league='ucl'")) == 1  # polling never opened a new UCL entry


def test_excluded_venue_blocks_soft_book_entries_too(http):
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    eng = engine(http, soft_edge=-1.0, min_edge=0.05, excluded_venues=["codere_it"], entry_sides=["yes"])
    eng.leagues = get_leagues(["nba"])
    eng.cycle(now)
    assert not eng.ledger.entries("venue='codere_it'")
    eng2 = engine(http, soft_edge=-1.0, min_edge=0.05, excluded_venues=[], entry_sides=["yes"])
    eng2.leagues = get_leagues(["nba"])
    eng2.cycle(now)
    assert eng2.ledger.entries("venue='codere_it'")


# ---- league priority + NFL support -----------------------------------------------------------------------------------------
def test_league_priority_order_favours_volume_markets():
    assert DEFAULT_ORDER == Settings().leagues
    assert DEFAULT_ORDER.index("nba") < DEFAULT_ORDER.index("nfl") < DEFAULT_ORDER.index("epl") < DEFAULT_ORDER.index("ligamx") < DEFAULT_ORDER.index("ucl")


def test_nfl_league_registered_with_both_venues_and_pinnacle_key():
    lg = LEAGUES["nfl"]
    assert (lg.polymarket_series_id, lg.kalshi_series, lg.odds_api_sport, lg.three_way) == ("12185", "KXNFLGAME", "americanfootball_nfl", False)


@pytest.mark.parametrize("name,expected", [
    ("Green Bay Packers", "gb"), ("Packers", "gb"), ("Green Bay", "gb"), ("Carolina Panthers", "car"), ("Panthers", "car"),
    ("Baltimore Ravens", "bal"), ("Buffalo Bills", "buf"), ("Los Angeles Chargers", "lac"), ("Los Angeles C", "lac"),
    ("Los Angeles Rams", "lar"), ("Los Angeles R", "lar"), ("New York Jets", "nyj"), ("New York J", "nyj"),
    ("New York Giants", "nyg"), ("San Francisco 49ers", "sf"), ("Las Vegas Raiders", "lv"), ("Tampa Bay Buccaneers", "tb"),
])
def test_nfl_aliases(name, expected):
    assert canonical(name, "nfl") == expected


def test_nfl_has_all_32_teams_unique():
    from clv_engine.teams import _tables

    assert len(set(_tables()["nfl"].values())) == 32


def test_nfl_recorded_fixtures_match_cross_venue_and_pinnacle(http):
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    lg = LEAGUES["nfl"]
    pm = PolymarketAdapter(http).fetch_games(lg, 120, now)
    ks = KalshiAdapter(http).fetch_games(lg, 120, now)
    assert {g.venue for g in pm + ks} == {"polymarket", "kalshi"}
    assert all(set(g.teams) <= {"car", "gb", "bal", "buf"} for g in pm + ks)
    src = make_sharp_source(Settings(), http)
    assert not src.available
    s = Settings(odds_api_key="k", sharp_source="theoddsapi")
    sharp = make_sharp_source(s, http).fetch(lg, now)
    games = match_games("nfl", False, pm + ks, sharp, 30)
    cross = {tuple(sorted(g.teams)): g for g in games if len(g.venues) == 2 and g.sharp and g.sharp.sharp}
    assert set(cross) == {("car", "gb"), ("bal", "buf")}
    for g in cross.values():
        assert sum(g.sharp.sharp.fair.values()) == pytest.approx(1.0)
        for vg in g.venues.values():
            assert set(vg.outcomes) == set(g.teams)
            if vg.venue == "polymarket":
                assert vg.fees.rate == 0.03  # NFL rate read from the market's feeSchedule, not assumed


def test_nfl_entry_end_to_end_clean_of_fee_surprises(http):
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    eng = engine(http, min_edge=0.05, excluded_venues=[], excluded_leagues=["ucl"], entry_sides=["yes"], soft_entries=False, lookahead_hours=120)
    eng.cycle(now)
    rows = eng.ledger.entries("league='nfl'")
    assert rows and {r["ref_source"] for r in rows} == {"pinnacle"}
    r = [x for x in rows if x["venue"] == "polymarket" and x["outcome_key"] == "car"][0]
    assert r["best_ask"] == pytest.approx(0.36)
    assert r["fee"] == pytest.approx(round(r["shares"] * 0.03 * 0.36 * 0.64, 5), abs=1e-3)  # NFL fee rate recorded in the fixture
    assert r["edge_entry"] >= 0.05 and r["partial"] == 0
