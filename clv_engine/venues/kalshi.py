"""Kalshi read-only adapter (public market data, no auth).

Endpoints (https://docs.kalshi.com, verified 2026-10-18; host api.elections.kalshi.com
serves all categories):
  GET /trade-api/v2/events?series_ticker=...&status=open&with_nested_markets=true
  GET /trade-api/v2/markets/orderbooks?tickers=A&tickers=B   (yes bids + no bids)
  GET /trade-api/v2/series/{ticker}                          (fee_type, fee_multiplier)
  GET /trade-api/v2/markets/{ticker}                         (result for settlement)
The order book returns bids only; a NO bid at p is a YES ask at 1-p.
GET only: no API key, no order endpoints.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from ..fees import FeeModel
from ..http import Http
from ..leagues import League
from ..models import DRAW, OutcomeQuote, VenueGame
from ..orderbook import complement
from ..teams import canonical, split_title
from ..util import parse_datetime, safe_float, utc_now

log = logging.getLogger(__name__)

BASE = "https://api.elections.kalshi.com/trade-api/v2"
VENUE = "kalshi"
TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def ticker_date(event_ticker: str) -> datetime | None:
    m = TICKER_DATE.search(event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    from datetime import timezone

    return datetime(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)), 12, tzinfo=timezone.utc)


class KalshiAdapter:
    def __init__(self, http: Http, base: str = BASE, fee_rounding: str = "cent"):
        self.http = http
        self.base = base.rstrip("/")
        self.fee_rounding = fee_rounding
        self._fee_mult: dict[str, float] = {}

    def fee_multiplier(self, series: str) -> float:
        if series not in self._fee_mult:
            data = self.http.get_json(f"{self.base}/series/{series}")
            mult = 1.0
            if isinstance(data, dict) and isinstance(data.get("series"), dict):
                mult = safe_float(data["series"].get("fee_multiplier"), 1.0)
            self._fee_mult[series] = mult
        return self._fee_mult[series]

    def list_events(self, league: League) -> list[dict[str, Any]]:
        if not league.kalshi_series:
            return []
        out: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            data = self.http.get_json(
                f"{self.base}/events",
                {"series_ticker": league.kalshi_series, "status": "open", "with_nested_markets": "true", "limit": 200, "cursor": cursor},
            )
            if not isinstance(data, dict):
                break
            out.extend(data.get("events") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def orderbooks(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        books: dict[str, dict[str, Any]] = {}
        for i in range(0, len(tickers), 20):
            chunk = tickers[i : i + 20]
            data = self.http.get_json(f"{self.base}/markets/orderbooks", [("tickers", t) for t in chunk])
            if not isinstance(data, dict):
                continue
            for ob in data.get("orderbooks") or []:
                fp = ob.get("orderbook_fp") or {}
                yes_bids = [(safe_float(p), safe_float(s)) for p, s in (fp.get("yes_dollars") or [])]
                no_bids = [(safe_float(p), safe_float(s)) for p, s in (fp.get("no_dollars") or [])]
                books[ob.get("ticker")] = {"yes_bids": yes_bids, "no_bids": no_bids}
        return books

    def parse_event(self, event: dict[str, Any], league: League) -> dict[str, Any] | None:
        labels = split_title(event.get("title") or "")
        markets = [m for m in event.get("markets") or [] if m.get("status") in ("active", "open", None)]
        if len(labels) != 2 or not markets:
            return None
        teams = [canonical(l, league.key) for l in labels]
        legs = []
        for m in markets:
            sub = (m.get("yes_sub_title") or "").strip()
            if sub.lower() in ("tie", "draw"):
                key, label = DRAW, "Draw"
            else:
                key, label = canonical(sub, league.key), sub
            legs.append({"key": key, "label": label, "ticker": m.get("ticker"), "price": safe_float(m.get("yes_ask_dollars"), 0.0) or None})
        # occurrence_datetime is not the kickoff (observed ~3h after start); use it only as a
        # coarse hint and fall back to the date encoded in the event ticker.
        start = parse_datetime(markets[0].get("occurrence_datetime"))
        if start is not None:
            start = start - timedelta(hours=3)
        else:
            start = ticker_date(event.get("event_ticker") or "")
        return {"event": event, "labels": labels, "teams": teams, "legs": legs, "start": start}

    def fetch_games(self, league: League, lookahead_hours: float = 72, now: datetime | None = None) -> list[VenueGame]:
        now = now or utc_now()
        sks = []
        for ev in self.list_events(league):
            sk = self.parse_event(ev, league)
            if not sk or not sk["start"]:
                continue
            if sk["start"] < now - timedelta(hours=3) or sk["start"] > now + timedelta(hours=lookahead_hours):
                continue
            sks.append(sk)
        books = self.orderbooks([leg["ticker"] for sk in sks for leg in sk["legs"]])
        mult = self.fee_multiplier(league.kalshi_series) if sks else 1.0
        games = []
        for sk in sks:
            outcomes = {}
            for leg in sk["legs"]:
                b = books.get(leg["ticker"], {"yes_bids": [], "no_bids": []})
                outcomes[leg["key"]] = OutcomeQuote(
                    key=leg["key"], label=leg["label"], ref=leg["ticker"],
                    yes_asks=complement(b["no_bids"]), yes_bids=b["yes_bids"], no_asks=complement(b["yes_bids"]),
                    no_ref=leg["ticker"], market_ref=leg["ticker"], display_price=leg["price"],
                )
            ev = sk["event"]
            games.append(VenueGame(
                venue=VENUE, league=league.key, ref=ev.get("event_ticker"), title=ev.get("title") or "",
                teams=sk["teams"], team_labels=sk["labels"], start=sk["start"], outcomes=outcomes,
                fees=FeeModel(VENUE, multiplier=mult, rounding=self.fee_rounding), three_way=league.three_way,
                url=f"https://kalshi.com/markets/{league.kalshi_series.lower()}",
            ))
        return games

    def resolution(self, ticker: str) -> float | None:
        data = self.http.get_json(f"{self.base}/markets/{ticker}")
        if not isinstance(data, dict):
            return None
        m = data.get("market") or {}
        result = (m.get("result") or "").lower()
        if result == "yes":
            return 1.0
        if result == "no":
            return 0.0
        if result in ("void", "scalar") and m.get("settlement_value_dollars") is not None:
            return safe_float(m.get("settlement_value_dollars"))
        return None
