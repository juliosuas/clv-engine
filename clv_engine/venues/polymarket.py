"""Polymarket read-only adapter.

Discovery: Gamma API events by sports series id (GET /events).
Prices: CLOB public order book (GET https://clob.polymarket.com/book?token_id=...).
No authentication, no order placement, no wallet. GET only.

normalize_market() is adapted from juliosuas/polymarket-desk (api/common.py, MIT);
the original EXCLUDE_PATTERNS that hid "X vs Y" matches is intentionally NOT used.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from ..fees import POLYMARKET_SPORTS_RATE, FeeModel
from ..http import Http
from ..leagues import League
from ..models import DRAW, OutcomeQuote, VenueGame
from ..teams import canonical, split_title
from ..util import parse_datetime, parse_json_field, safe_float, utc_now

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
VENUE = "polymarket"


def normalize_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    fee = raw.get("feeSchedule") or {}
    rate = safe_float(fee.get("rate"), POLYMARKET_SPORTS_RATE) if isinstance(fee, dict) and fee else POLYMARKET_SPORTS_RATE
    if raw.get("feesEnabled") is False:
        rate = 0.0
    return {
        "id": raw.get("id"),
        "condition_id": raw.get("conditionId"),
        "question": (raw.get("question") or "").strip(),
        "slug": raw.get("slug"),
        "outcomes": parse_json_field(raw.get("outcomes"), []),
        "prices": [safe_float(p) for p in parse_json_field(raw.get("outcomePrices"), [])],
        "clob_token_ids": parse_json_field(raw.get("clobTokenIds"), []),
        "best_bid": safe_float(raw.get("bestBid")),
        "best_ask": safe_float(raw.get("bestAsk")),
        "group_title": raw.get("groupItemTitle") or "",
        "market_type": raw.get("sportsMarketType"),
        "start": parse_datetime(raw.get("gameStartTime")),
        "fee_rate": rate,
        "closed": bool(raw.get("closed")),
        "active": raw.get("active") is not False,
        "neg_risk": bool(raw.get("negRisk")),
    }


class PolymarketAdapter:
    def __init__(self, http: Http, fetch_no_books: bool = True, max_workers: int = 6):
        self.http = http
        self.fetch_no_books = fetch_no_books
        self.max_workers = max_workers

    # ---- discovery -------------------------------------------------------
    def list_events(self, league: League, now: datetime | None = None) -> list[dict[str, Any]]:
        if not league.polymarket_series_id:
            return []
        now = now or utc_now()
        events: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.http.get_json(
                f"{GAMMA}/events",
                {
                    "series_id": league.polymarket_series_id,
                    "closed": "false",
                    "end_date_min": (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": 100,
                    "offset": offset,
                },
            )
            if not isinstance(page, list) or not page:
                break
            events.extend(page)
            if len(page) < 100 or offset >= 900:
                break
            offset += 100
        return events

    def book(self, token_id: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        data = self.http.get_json(f"{CLOB}/book", {"token_id": token_id})
        if not isinstance(data, dict):
            return [], []
        asks = [(safe_float(l.get("price")), safe_float(l.get("size"))) for l in data.get("asks") or []]
        bids = [(safe_float(l.get("price")), safe_float(l.get("size"))) for l in data.get("bids") or []]
        return asks, bids

    # ---- parsing ---------------------------------------------------------
    def parse_event(self, event: dict[str, Any], league: League) -> dict[str, Any] | None:
        """Return a game skeleton (no books yet) for an event with a moneyline."""
        markets = [m for m in (normalize_market(x) for x in event.get("markets") or []) if m and m["market_type"] == "moneyline"]
        markets = [m for m in markets if m["active"] and not m["closed"]]
        if not markets:
            return None
        labels = split_title(event.get("title") or "")
        if len(labels) != 2:
            return None
        teams = [canonical(l, league.key) for l in labels]
        start = markets[0]["start"] or parse_datetime(event.get("startTime"))
        legs: list[dict[str, Any]] = []
        if len(markets) == 1 and len(markets[0]["outcomes"]) == 2 and markets[0]["outcomes"][0].lower() not in ("yes", "no"):
            # two-way market, one token per team (NBA style); complement of A is B
            m = markets[0]
            for i, name in enumerate(m["outcomes"]):
                key = canonical(name, league.key)
                legs.append({"key": key, "label": name, "yes": m["clob_token_ids"][i], "no": None, "market": m["condition_id"], "price": m["prices"][i] if i < len(m["prices"]) else None, "rate": m["fee_rate"]})
        else:
            # neg-risk group of Yes/No markets (soccer 1X2: team A / draw / team B)
            for m in markets:
                if len(m["clob_token_ids"]) != 2:
                    continue
                title = m["group_title"]
                if title.lower().startswith("draw") or "draw" in m["question"].lower():
                    key, label = DRAW, "Draw"
                else:
                    key, label = canonical(title, league.key), title
                legs.append({"key": key, "label": label, "yes": m["clob_token_ids"][0], "no": m["clob_token_ids"][1], "market": m["condition_id"], "price": m["prices"][0] if m["prices"] else None, "rate": m["fee_rate"]})
        return {"event": event, "labels": labels, "teams": teams, "start": start, "legs": legs, "rate": markets[0]["fee_rate"]}

    def fetch_games(self, league: League, lookahead_hours: float = 72, now: datetime | None = None) -> list[VenueGame]:
        now = now or utc_now()
        skeletons = []
        for ev in self.list_events(league, now):
            sk = self.parse_event(ev, league)
            if not sk or not sk["start"]:
                continue
            if sk["start"] < now or sk["start"] > now + timedelta(hours=lookahead_hours):
                continue
            skeletons.append(sk)
        tokens: list[str] = []
        for sk in skeletons:
            for leg in sk["legs"]:
                tokens.append(leg["yes"])
                if leg["no"] and self.fetch_no_books:
                    tokens.append(leg["no"])
        books: dict[str, Any] = {}
        if tokens:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                for tok, res in zip(tokens, pool.map(self.book, tokens)):
                    books[tok] = res
        return [self._build(sk, league, books) for sk in skeletons]

    def _build(self, sk: dict[str, Any], league: League, books: dict[str, Any]) -> VenueGame:
        outcomes: dict[str, OutcomeQuote] = {}
        for leg in sk["legs"]:
            yes_asks, yes_bids = books.get(leg["yes"], ([], []))
            no_asks: list = []
            if leg["no"]:
                no_asks, _ = books.get(leg["no"], ([], []))
            outcomes[leg["key"]] = OutcomeQuote(
                key=leg["key"], label=leg["label"], ref=leg["yes"], yes_asks=yes_asks, yes_bids=yes_bids,
                no_asks=no_asks, no_ref=leg["no"], market_ref=leg["market"], display_price=leg["price"],
            )
        ev = sk["event"]
        return VenueGame(
            venue=VENUE, league=league.key, ref=ev.get("slug") or str(ev.get("id")), title=ev.get("title") or "",
            teams=sk["teams"], team_labels=sk["labels"], start=sk["start"], outcomes=outcomes,
            fees=FeeModel(VENUE, rate=sk["rate"]), three_way=league.three_way,
            url=f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else None,
        )

    # ---- settlement ------------------------------------------------------
    def resolution(self, event_slug: str, outcome_ref: str) -> float | None:
        """Settlement value (0..1) of the YES token `outcome_ref`, or None if unresolved."""
        data = self.http.get_json(f"{GAMMA}/events", {"slug": event_slug})
        if not isinstance(data, list) or not data:
            return None
        for raw in data[0].get("markets") or []:
            m = normalize_market(raw)
            if not m or outcome_ref not in m["clob_token_ids"]:
                continue
            if not m["closed"]:
                return None
            idx = m["clob_token_ids"].index(outcome_ref)
            prices = m["prices"]
            if idx < len(prices) and prices and all(p in (0.0, 0.5, 1.0) for p in prices):
                return prices[idx]
            return None
        return None
