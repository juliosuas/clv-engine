"""The Odds API v4 adapter (https://the-odds-api.com/liveapi/guides/v4/).

  GET /v4/sports/{sport}/odds?apiKey=...&markets=h2h&oddsFormat=decimal&bookmakers=pinnacle,...
  GET /v4/historical/sports/{sport}/odds?date=...   (paid plans; optional closing backfill)

Pinnacle ("pinnacle", EU region) is the sharp reference. Soft books come from
SOFT_BOOKS (comma list of Odds API bookmaker keys). As of 2026-10-18 the Odds API
bookmaker list has no Caliente and only "codere_it" (Codere Italy), not Codere MX.

The key is read from ODDS_API_KEY only and is redacted from logs.
Credits: one /odds call costs (#markets) x (#regions); a bookmakers list counts
as one region per 10 books. Polling is throttled by SHARP_POLL_SECONDS.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from ..devig import devig
from ..http import Http
from ..leagues import League
from ..models import DRAW, BookLine, SharpGame
from ..teams import canonical
from ..util import parse_datetime, safe_float

log = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4"


def parse_book(bm: dict[str, Any], home: str, away: str, home_label: str, away_label: str, league: League, method: str) -> BookLine | None:
    for market in bm.get("markets") or []:
        if market.get("key") != "h2h":
            continue
        odds: dict[str, float] = {}
        for o in market.get("outcomes") or []:
            name = o.get("name") or ""
            price = safe_float(o.get("price"), 0.0)
            if price <= 1.0:
                continue
            if name.lower() in ("draw", "tie"):
                odds[DRAW] = price
            elif name == home_label:
                odds[home] = price
            elif name == away_label:
                odds[away] = price
            else:
                odds[canonical(name, league.key)] = price
        need = 3 if league.three_way else 2
        if len(odds) != need:
            return None
        keys = list(odds)
        fair = dict(zip(keys, devig([odds[k] for k in keys], method)))
        return BookLine(book=bm.get("key") or "", odds=odds, fair=fair, last_update=parse_datetime(market.get("last_update") or bm.get("last_update")))
    return None


def parse_events(data: Any, league: League, method: str, sharp_book: str, soft_books: list[str]) -> list[SharpGame]:
    games: list[SharpGame] = []
    if not isinstance(data, list):
        return games
    for ev in data:
        hl, al = ev.get("home_team") or "", ev.get("away_team") or ""
        home, away = canonical(hl, league.key), canonical(al, league.key)
        sharp = None
        soft: dict[str, BookLine] = {}
        for bm in ev.get("bookmakers") or []:
            line = parse_book(bm, home, away, hl, al, league, method)
            if not line:
                continue
            if line.book == sharp_book:
                sharp = line
            elif line.book in soft_books:
                soft[line.book] = line
        games.append(SharpGame(league=league.key, ref=ev.get("id") or "", home=home, away=away, home_label=hl, away_label=al,
                               start=parse_datetime(ev.get("commence_time")), sharp=sharp, soft=soft))
    return games


class TheOddsApiSource:
    name = "theoddsapi"
    available = True

    def __init__(self, http: Http, api_key: str, settings):
        self.http = http
        self._key = api_key
        self.method = settings.devig_method
        self.sharp_book = settings.sharp_book
        self.soft_books = settings.soft_books
        self.poll_seconds = settings.sharp_poll_seconds
        self.close_poll_seconds = settings.sharp_close_poll_seconds
        self.close_window_min = settings.close_capture_minutes
        self.historical = settings.odds_api_historical
        self._cache: dict[str, tuple[float, list[SharpGame]]] = {}

    def _books_param(self) -> str:
        return ",".join([self.sharp_book, *self.soft_books])

    def _due(self, league: League, now: datetime) -> bool:
        cached = self._cache.get(league.key)
        if not cached:
            return True
        age = time.monotonic() - cached[0]
        near_close = any(g.start and timedelta(0) <= g.start - now <= timedelta(minutes=self.close_window_min) for g in cached[1])
        return age >= (self.close_poll_seconds if near_close else self.poll_seconds)

    def fetch(self, league: League, now: datetime | None = None) -> list[SharpGame]:
        if not league.odds_api_sport:
            return []
        from ..util import utc_now

        now = now or utc_now()
        if not self._due(league, now):
            return self._cache[league.key][1]
        data = self.http.get_json(
            f"{BASE}/sports/{league.odds_api_sport}/odds",
            {"apiKey": self._key, "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso", "bookmakers": self._books_param()},
        )
        if data is None:
            return self._cache.get(league.key, (0, []))[1]
        games = parse_events(data, league, self.method, self.sharp_book, self.soft_books)
        self._cache[league.key] = (time.monotonic(), games)
        return games

    def closing(self, league: League, game_ref: str, start: datetime) -> SharpGame | None:
        if not self.historical or not league.odds_api_sport:
            return None
        when = (start - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self.http.get_json(
            f"{BASE}/historical/sports/{league.odds_api_sport}/events/{game_ref}/odds",
            {"apiKey": self._key, "markets": "h2h", "oddsFormat": "decimal", "date": when, "bookmakers": self.sharp_book},
        )
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return None
        games = parse_events([data["data"]], league, self.method, self.sharp_book, [])
        return games[0] if games and games[0].sharp else None
