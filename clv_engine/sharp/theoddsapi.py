"""The Odds API v4 adapter (https://the-odds-api.com/liveapi/guides/v4/).

  GET /v4/sports/{sport}/odds?apiKey=...&markets=h2h&oddsFormat=decimal&bookmakers=pinnacle,...
  GET /v4/historical/sports/{sport}/odds?date=...   (paid plans; optional closing backfill)

Pinnacle ("pinnacle", EU region) is the sharp reference. Soft books come from
SOFT_BOOKS (comma list of Odds API bookmaker keys). As of 2026-10-18 the Odds API
bookmaker list has no Caliente and only "codere_it" (Codere Italy), not Codere MX.

The key is read from ODDS_API_KEY only and is redacted from logs.
Credits: one /odds call costs (#markets) x (#regions); a bookmakers list counts
as one region per 10 books. Polling cadence and the daily credit budget are handled by
sharp/quota.py (tiers by time to kickoff, ODDS_API_DAILY_BUDGET, x-requests-* headers).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ..devig import devig
from ..http import Http
from ..leagues import League
from ..models import DRAW, BookLine, SharpGame
from ..teams import canonical
from ..util import parse_datetime, safe_float, utc_now
from .quota import CLOSE, HISTORICAL_COST_MULTIPLIER, QuotaTracker, plan_league

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

    def __init__(self, http: Http, api_key: str, settings, ledger=None):
        self.http = http
        self._key = api_key
        self.s = settings
        self.method = settings.devig_method
        self.sharp_book = settings.sharp_book
        self.soft_books = settings.soft_books
        self.historical = settings.odds_api_historical
        self.quota = QuotaTracker(settings, ledger, n_books=1 + len(settings.soft_books))
        self._cache: dict[str, tuple[datetime, list[SharpGame]]] = {}
        self._pending: dict[str, list[datetime]] = {}
        self._retry_after: dict[str, datetime] = {}
        self._warned: set[tuple[str, str, str]] = set()
        self.tier: dict[str, str] = {}
        self.closing_blocked = False

    def _books_param(self) -> str:
        return ",".join([self.sharp_book, *self.soft_books])

    # ---- scheduling hooks ------------------------------------------------
    def set_pending(self, league_key: str, kickoffs: list[datetime]) -> None:
        self._pending[league_key] = list(kickoffs)

    def fetched_at(self, league_key: str) -> datetime | None:
        cached = self._cache.get(league_key)
        return cached[0] if cached else None

    def _plan(self, league: League, now: datetime) -> tuple[str, int]:
        cached = self._cache.get(league.key)
        known = [g.start for g in cached[1] if g.start] if cached else []
        tier, interval, _ = plan_league(now, known, self._pending.get(league.key, []), self.s)
        self.tier[league.key] = tier
        return tier, interval

    def _due(self, league: League, now: datetime) -> tuple[bool, str]:
        tier, interval = self._plan(league, now)
        cached = self._cache.get(league.key)
        retry = self._retry_after.get(league.key)
        if retry and now < retry:
            return False, tier
        if not cached:
            return True, tier
        slack = min(15.0, interval * 0.1)  # the engine loop is not perfectly periodic
        return (now - cached[0]).total_seconds() >= interval - slack, tier

    def _warn_once(self, league: League, now: datetime, reason: str) -> None:
        key = (league.key, self.quota.day(now), reason.split("(")[0].strip()[:40])
        if key not in self._warned:
            self._warned.add(key)
            log.warning("Odds API poll skipped for %s: %s; serving cached Pinnacle data (stale data is not used for entries "
                        "or closing capture after MAX_SHARP_AGE_MINUTES)", league.key, reason)

    def fetch(self, league: League, now: datetime | None = None) -> list[SharpGame]:
        if not league.odds_api_sport:
            return []
        now = now or utc_now()
        due, tier = self._due(league, now)
        cached_games = self._cache[league.key][1] if league.key in self._cache else []
        if not due:
            return cached_games
        ok, reason = self.quota.decide(now, essential=(tier == CLOSE))
        if not ok:
            self.quota.record_skip(now)
            self._warn_once(league, now, reason)
            return cached_games
        data, headers = self._get(f"{BASE}/sports/{league.odds_api_sport}/odds",
                                  {"apiKey": self._key, "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso",
                                   "bookmakers": self._books_param()})
        if data is not None or headers:  # a transport failure with no response is not charged
            self.quota.record_call(now, headers)
        if data is None:
            self._retry_after[league.key] = now + timedelta(seconds=min(300, max(60, self.s.poll_seconds)))
            log.warning("Odds API poll failed for %s (tier %s); retrying later, remaining=%s", league.key, tier, self.quota.last_remaining)
            return cached_games
        games = parse_events(data, league, self.method, self.sharp_book, self.soft_books)
        self._cache[league.key] = (now, games)
        return games

    def _get(self, url: str, params: dict[str, Any]) -> tuple[Any, dict[str, str]]:
        meta = getattr(self.http, "get_json_meta", None)
        if meta is not None:
            return meta(url, params)
        return self.http.get_json(url, params), {}

    def closing(self, league: League, game_ref: str, start: datetime, now: datetime | None = None) -> SharpGame | None:
        """Historical closing-line backfill (paid plans; documented cost x10): budgeted like any other call."""
        self.closing_blocked = False
        if not self.historical or not league.odds_api_sport:
            return None
        now = now or utc_now()
        ok, reason = self.quota.decide(now, essential=False, multiplier=HISTORICAL_COST_MULTIPLIER)
        if not ok:
            self.closing_blocked = True
            self.quota.record_skip(now)
            self._warn_once(league, now, "historical backfill " + reason)
            return None
        when = (start - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data, headers = self._get(f"{BASE}/historical/sports/{league.odds_api_sport}/events/{game_ref}/odds",
                                  {"apiKey": self._key, "markets": "h2h", "oddsFormat": "decimal", "date": when, "bookmakers": self.sharp_book})
        self.quota.record_call(now, headers, multiplier=HISTORICAL_COST_MULTIPLIER)
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return None
        games = parse_events([data["data"]], league, self.method, self.sharp_book, [])
        return games[0] if games and games[0].sharp else None
