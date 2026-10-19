from datetime import datetime, timedelta, timezone

import pytest

from clv_engine.fees import FeeModel
from clv_engine.leagues import LEAGUES
from clv_engine.matching import match_games, teams_match, times_close
from clv_engine.models import VenueGame
from clv_engine.teams import canonical, normalize, same_team, split_title
from clv_engine.venues.kalshi import KalshiAdapter
from clv_engine.venues.polymarket import PolymarketAdapter


@pytest.mark.parametrize("a,b,league", [
    ("CD Guadalajara", "Guadalajara", "ligamx"), ("Chivas", "Guadalajara", "ligamx"),
    ("Club Santos Laguna", "Santos", "ligamx"), ("Querétaro FC", "Queretaro", "ligamx"),
    ("Pumas de la UNAM", "UNAM", "ligamx"), ("Tigres de la UANL", "Tigres", "ligamx"),
    ("Celtics", "Boston", "nba"), ("76ers", "Philadelphia", "nba"), ("Knicks", "New York", "nba"),
    ("Los Angeles L", "Lakers", "nba"), ("Rayo Vallecano de Madrid", "Vallecano", "laliga"),
    ("Real Racing Club", "Santander", "laliga"), ("FK Bodø/Glimt", "Bodoe/Glimt", "ucl"),
    ("Fenerbahçe SK", "Fenerbahce", "ucl"), ("Manchester City FC", "Man City", "epl"),
])
def test_aliases_resolve_same_team(a, b, league):
    assert canonical(a, league) == canonical(b, league)


def test_distinct_teams_do_not_collide():
    assert canonical("Real Madrid", "laliga") != canonical("Atlético de Madrid", "laliga")
    assert canonical("Manchester United", "epl") != canonical("Manchester City", "epl")
    assert canonical("Los Angeles C", "nba") != canonical("Los Angeles L", "nba")
    assert canonical("Atlético San Luis", "ligamx") != canonical("Santos Laguna", "ligamx")


def test_unknown_team_fallback_containment():
    a, b = canonical("FC Zorya Luhanskx", "ucl"), canonical("Zorya Luhanskx", "ucl")
    assert a.startswith("x:") and same_team(a, b)


def test_normalize_and_split():
    assert normalize("Club América") == "america"
    assert split_title("Querétaro FC vs. CF Monterrey") == ["Querétaro FC", "CF Monterrey"]
    assert split_title("Boston vs Detroit") == ["Boston", "Detroit"]
    assert split_title("CF América vs. Club Santos Laguna - Total Corners") == ["CF América", "Club Santos Laguna"]


def test_time_window():
    t = datetime(2026, 10, 20, 19, tzinfo=timezone.utc)
    assert times_close(t, t + timedelta(hours=3), 30)
    assert not times_close(t, t + timedelta(days=3), 30)
    assert teams_match(["bos", "det"], ["det", "bos"])


def _vg(venue, teams, start, league="nba"):
    return VenueGame(venue, league, f"{venue}-{teams}", "", teams, teams, start, {}, FeeModel(venue), False)


def test_same_teams_different_week_not_merged():
    t = datetime(2026, 10, 20, 19, tzinfo=timezone.utc)
    games = match_games("nba", False, [_vg("polymarket", ["bos", "det"], t), _vg("kalshi", ["det", "bos"], t + timedelta(days=6))], [], 30)
    assert len(games) == 2


def test_recorded_fixtures_match_cross_venue(http, now):
    for lk in ("nba", "ligamx"):
        lg = LEAGUES[lk]
        pm = PolymarketAdapter(http).fetch_games(lg, 72, now)
        ks = KalshiAdapter(http).fetch_games(lg, 72, now)
        games = match_games(lk, lg.three_way, pm + ks, [], 30)
        cross = [g for g in games if len(g.venues) == 2]
        if lk == "nba":
            assert {tuple(sorted(g.teams)) for g in cross} == {("bos", "det"), ("nyk", "phi")}
        else:
            assert [tuple(sorted(g.teams)) for g in cross] == [("guadalajara", "necaxa")]
            g = cross[0]
            assert set(g.venues["polymarket"].outcomes) == set(g.venues["kalshi"].outcomes) == {"guadalajara", "necaxa", "draw"}
