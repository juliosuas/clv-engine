"""League registry: ids on each venue and on The Odds API.

Polymarket series ids come from GET https://gamma-api.polymarket.com/sports
(field "series"); Kalshi series tickers from GET /trade-api/v2/series?category=Sports;
Odds API sport keys from https://the-odds-api.com/sports-odds-data/sports-apis.html.
All verified 2026-10-18.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class League:
    key: str
    name: str
    sport: str  # soccer | basketball
    three_way: bool
    polymarket_series_id: str | None
    polymarket_ordering: str  # "home" -> first listed team is home; "away" -> first is away
    kalshi_series: str | None
    odds_api_sport: str | None


LEAGUES: dict[str, League] = {
    "ligamx": League("ligamx", "Liga MX", "soccer", True, "10290", "home", "KXLIGAMXGAME", "soccer_mexico_ligamx"),
    "epl": League("epl", "Premier League", "soccer", True, "10188", "home", "KXEPLGAME", "soccer_epl"),
    "laliga": League("laliga", "La Liga", "soccer", True, "10193", "home", "KXLALIGAGAME", "soccer_spain_la_liga"),
    "ucl": League("ucl", "UEFA Champions League", "soccer", True, "10204", "home", "KXUCLGAME", "soccer_uefa_champs_league"),
    "nba": League("nba", "NBA", "basketball", False, "10345", "away", "KXNBAGAME", "basketball_nba"),
}

DEFAULT_ORDER = ["ligamx", "epl", "laliga", "ucl", "nba"]


def get_leagues(keys: list[str]) -> list[League]:
    out = []
    for k in keys:
        k = k.strip().lower()
        if not k:
            continue
        if k not in LEAGUES:
            raise ValueError(f"unknown league {k!r}; known: {', '.join(LEAGUES)}")
        out.append(LEAGUES[k])
    return out
