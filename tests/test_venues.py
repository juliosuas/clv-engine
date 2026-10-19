import pytest

from clv_engine.leagues import LEAGUES
from clv_engine.venues.kalshi import KalshiAdapter, ticker_date
from clv_engine.venues.polymarket import PolymarketAdapter, normalize_market


def test_polymarket_nba_two_way(http, now):
    games = PolymarketAdapter(http).fetch_games(LEAGUES["nba"], 72, now)
    g = next(g for g in games if g.ref == "nba-bos-det-2026-10-20")
    assert g.teams == ["bos", "det"] and not g.three_way
    assert g.outcomes["bos"].best_ask == pytest.approx(0.52)
    assert g.outcomes["det"].best_ask == pytest.approx(0.49)
    assert g.outcomes["bos"].best_bid == pytest.approx(0.51)
    assert g.fees.venue == "polymarket" and g.fees.rate == pytest.approx(0.05)
    assert g.exhaustive


def test_polymarket_soccer_neg_risk_with_draw(http, now):
    games = PolymarketAdapter(http).fetch_games(LEAGUES["ligamx"], 72, now)
    assert [g.ref for g in games] == ["mex-gua-nec-2026-10-20"]  # Nov 14 game is outside the 72h lookahead
    g = games[0]
    assert set(g.outcomes) == {"guadalajara", "draw", "necaxa"}
    assert g.outcomes["guadalajara"].best_ask == pytest.approx(0.70)
    assert g.outcomes["necaxa"].best_ask == pytest.approx(0.14)
    assert g.outcomes["necaxa"].no_asks[0][0] == pytest.approx(0.87)
    assert g.start.isoformat().startswith("2026-10-21T00:07")


def test_normalize_market_fee_schedule():
    m = normalize_market({"feeSchedule": {"rate": 0.04}, "outcomes": "[\"Yes\",\"No\"]", "clobTokenIds": "[\"1\",\"2\"]"})
    assert m["fee_rate"] == 0.04 and m["clob_token_ids"] == ["1", "2"]
    assert normalize_market({"feesEnabled": False})["fee_rate"] == 0.0


def test_kalshi_asks_from_opposite_bids(http, now):
    games = KalshiAdapter(http).fetch_games(LEAGUES["nba"], 72, now)
    g = next(g for g in games if g.ref == "KXNBAGAME-26OCT20BOSDET")
    bos = g.outcomes["bos"]
    assert bos.best_ask == pytest.approx(1 - 0.48)  # best NO bid 0.48 -> YES ask 0.52
    assert bos.best_bid == pytest.approx(0.51)
    assert bos.no_asks[0][0] == pytest.approx(1 - 0.51)
    assert g.fees.venue == "kalshi" and g.fees.multiplier == 1


def test_kalshi_soccer_tie_is_draw(http, now):
    games = KalshiAdapter(http).fetch_games(LEAGUES["ligamx"], 72, now)
    g = next(g for g in games if "CDGNCX" in g.ref)
    assert set(g.outcomes) == {"guadalajara", "necaxa", "draw"}


def test_ticker_date():
    assert ticker_date("KXNBAGAME-26OCT20BOSDET").date().isoformat() == "2026-10-20"
