"""The Odds API v4 adapter (https://the-odds-api.com/liveapi/guides/v4/).

  GET /v4/sports/{sport}/odds?apiKey=...&markets=h2h&oddsFormat=decimal&bookmakers=pinnacle,...
  GET /v4/historical/sports/{sport}/odds?date=...   (paid plans; optional closing backfill)

Pinnacle ("pinnacle", EU region) is the sharp reference. Soft books come from
SOFT_BOOKS (comma list of Odds API bookmaker keys). As of 2026-10-18 the Odds API
bookmaker list has no Caliente and only "codere_it" (Codere Italy), not Codere MX.

The key is read from ODDS_API_KEY only and is redacted from logs.
Credits: one /odds call costs (#markets) x (#regions); a bookmakers list counts
as one region per 10 books, and `bookmakers` takes priority over `regions`. Polling is
scheduled by sharp/quota.py: one poll per sport serves every event of a kickoff round
(entry poll at T-25 min, close poll(s) at T-5..T-1 min), under a monthly budget with a hard cap.
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
from .quota import (
    ENTRY,
    FINAL,
    HISTORICAL,
    HISTORICAL_COST_MULTIPLIER,
    QuotaTracker,
    build_rounds,
    next_poll,
)

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


SCHEDULE_MERGE_MINUTES = 45


class TheOddsApiSource:
    name = "theoddsapi"
    available = True

    def __init__(self, http: Http, api_key: str, settings, ledger=None):
        self.http = http
        self._key = api_key
        self.s = settings
        self.ledger = ledger
        self.method = settings.devig_method
        self.sharp_book = settings.sharp_book
        self.soft_books = settings.soft_books
        self.historical = settings.odds_api_historical
        self.quota = QuotaTracker(settings, ledger, n_books=len(self._books_list()))
        self._cache: dict[str, tuple[datetime, list[SharpGame]]] = {}
        self._pending: dict[str, list[datetime]] = {}
        self._schedule: dict[str, list[datetime]] = {}
        self._polls: dict[str, list[datetime]] = {}
        self._retry_after: dict[str, datetime] = {}
        self._warned: set[tuple] = set()
        self.tier: dict[str, str] = {}  # last scheduler decision per league: entry | close_early | close_final | idle
        self.closing_blocked = False

    def _books_list(self) -> list[str]:
        """Pinnacle only, unless soft-book entries are enabled (then the soft books ride along: up to 10 books still cost 1 credit)."""
        return [self.sharp_book, *(self.soft_books if self.s.soft_entries else [])]

    def _books_param(self) -> str:
        return ",".join(self._books_list())

    # ---- scheduling hooks ------------------------------------------------
    def set_pending(self, league_key: str, kickoffs: list[datetime]) -> None:
        self._pending[league_key] = list(kickoffs)

    def set_schedule(self, league_key: str, kickoffs: list[datetime]) -> None:
        self._schedule[league_key] = list(kickoffs)

    def fetched_at(self, league_key: str) -> datetime | None:
        cached = self._cache.get(league_key)
        return cached[0] if cached else None

    def _rounds(self, league: League, now: datetime):
        cached = self._cache.get(league.key)
        known = [g.start for g in cached[1] if g.start] if cached else []
        # Pinnacle's commence_time is the truth; a venue kickoff (Kalshi's is only derived) that sits within SCHEDULE_MERGE_MINUTES
        # of one is the same game, so it must not open a second round (= a second entry poll)
        extra = [k for k in self._schedule.get(league.key, []) if not any(abs((k - p).total_seconds()) <= SCHEDULE_MERGE_MINUTES * 60 for p in known)]
        return build_rounds(now, known + extra, self._pending.get(league.key, []), self.s)

    def _committed(self, now: datetime) -> float:
        """Credits owed to the final close polls of every pending round (all sports) that has not had its close poll yet."""
        n = 0
        for lk, pend in self._pending.items():
            league_polls = self._polls.get(lk, [])
            for r in build_rounds(now, [], pend, self.s):
                lo = r.start - timedelta(minutes=self.s.close_window_max_minutes)
                if r.pending and not any(lo <= p <= r.start for p in league_polls):
                    n += 1
        return n * self.quota.est_cost

    def _due(self, league: League, now: datetime):
        retry = self._retry_after.get(league.key)
        due = None if (retry and now < retry) else next_poll(now, self._rounds(league, now), self.fetched_at(league.key), self._polls.get(league.key, []), self.s)
        self.tier[league.key] = due.kind if due else "idle"
        return due

    def _warn_once(self, key: tuple, msg: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    def fetch(self, league: League, now: datetime | None = None) -> list[SharpGame]:
        if not league.odds_api_sport:
            return []
        now = now or utc_now()
        due = self._due(league, now)
        cached_games = self._cache[league.key][1] if league.key in self._cache else []
        if due is None:
            return cached_games
        ok, reason = self.quota.decide(now, due.kind, committed=0.0 if due.kind == FINAL else self._committed(now))
        if not ok:
            self.quota.record_skip(now)
            self._deny(league, due, now, reason)
            return cached_games
        data, headers = self._get(f"{BASE}/sports/{league.odds_api_sport}/odds",
                                  {"apiKey": self._key, "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso",
                                   "regions": self.s.odds_api_regions, "bookmakers": self._books_param()})
        if data is not None or headers:  # a transport failure with no response is not charged
            self.quota.record_call(now, headers)
        if data is None:
            wait = 30 if due.kind != ENTRY else min(300, max(60, self.s.poll_seconds))
            self._retry_after[league.key] = now + timedelta(seconds=wait)
            log.warning("Odds API poll failed for %s (%s round %s); retrying later, remaining=%s", league.key, due.kind,
                        due.round.start.strftime("%Y-%m-%dT%H:%MZ"), self.quota.last_remaining)
            return cached_games
        games = parse_events(data, league, self.method, self.sharp_book, self.soft_books)
        self._cache[league.key] = (now, games)
        self._polls.setdefault(league.key, []).append(now)
        del self._polls[league.key][:-50]
        if due.kind != ENTRY and self.ledger is not None:
            self.ledger.clear_missed_close(league.key, due.round.start)  # a later cycle did get the close after all
        return games

    def _deny(self, league: League, due, now: datetime, reason: str) -> None:
        rnd = due.round.start.strftime("%Y-%m-%dT%H:%MZ")
        if due.kind == ENTRY:
            self._warn_once((league.key, "entry", rnd), "Odds API entry poll skipped for %s round %s: %s; entries for it may rely on stale Pinnacle data "
                            "(not used after MAX_SHARP_AGE_MINUTES)", league.key, rnd, reason)
        elif due.kind == FINAL:
            lo = due.round.start - timedelta(minutes=self.s.close_window_max_minutes)
            if any(lo <= p <= due.round.start for p in self._polls.get(league.key, [])):
                self._warn_once((league.key, "final-kept", rnd), "Odds API final close poll skipped for %s round %s: %s; keeping the earlier in-window sample as the close",
                                league.key, rnd, reason)
                return
            if self.ledger is not None:
                self.ledger.add_missed_close(league.key, due.round.start, due.round.end, now, reason)
            self._warn_once((league.key, "final", rnd), "Odds API close poll NOT made for %s round %s (kickoff %s): %s; entries of this round will be marked no_close "
                            "(excluded from CLV stats)", league.key, rnd, rnd, reason)
        else:
            self._warn_once((league.key, "early", rnd), "Odds API early close poll skipped for %s round %s: %s; degrading to a single final close poll",
                            league.key, rnd, reason)

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
        ok, reason = self.quota.decide(now, HISTORICAL, multiplier=HISTORICAL_COST_MULTIPLIER, committed=self._committed(now))
        if not ok:
            self.closing_blocked = True
            self.quota.record_skip(now)
            self._warn_once((league.key, "historical", self.quota.day(now)), "Odds API historical backfill skipped for %s: %s", league.key, reason)
            return None
        when = (start - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data, headers = self._get(f"{BASE}/historical/sports/{league.odds_api_sport}/events/{game_ref}/odds",
                                  {"apiKey": self._key, "markets": "h2h", "oddsFormat": "decimal", "date": when, "bookmakers": self.sharp_book})
        self.quota.record_call(now, headers, multiplier=HISTORICAL_COST_MULTIPLIER)
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return None
        games = parse_events([data["data"]], league, self.method, self.sharp_book, [])
        return games[0] if games and games[0].sharp else None
