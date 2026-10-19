import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests must never touch the network."""
    def guard(*a, **k):
        raise RuntimeError("network access attempted in tests")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


def load(name):
    return json.loads((FIX / name).read_text())


class FakeHttp:
    """Routes adapter GETs to recorded fixtures. Unknown URLs return None (like a failed GET)."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}
        self.pm_books = {**load("pm_books_nba.json"), **load("pm_books_ligamx.json")}

    def get_json(self, url, params=None, headers=None):
        q = {}
        if isinstance(params, dict):
            q = {k: v for k, v in params.items() if v is not None}
        elif params:
            for k, v in params:
                q.setdefault(k, []).append(v)
        self.calls.append((url, q))
        path = urlsplit(url).path
        for key, value in self.overrides.items():
            if key in url and (not isinstance(value, tuple) or all(q.get(k) == v for k, v in value[0].items())):
                return value[1] if isinstance(value, tuple) else value
        if "gamma-api.polymarket.com" in url and path == "/events":
            if "slug" in q:
                return None
            return load({"10345": "pm_events_nba.json", "10290": "pm_events_ligamx.json"}.get(str(q.get("series_id")), "missing")) if q.get("offset", 0) == 0 and str(q.get("series_id")) in ("10345", "10290") else []
        if "clob.polymarket.com" in url and path == "/book":
            b = self.pm_books.get(q.get("token_id"))
            return b
        if "kalshi.com" in url:
            if path.endswith("/events"):
                name = {"KXNBAGAME": "k_events_nba.json", "KXLIGAMXGAME": "k_events_ligamx.json"}.get(q.get("series_ticker"))
                return load(name) if name else {"events": [], "cursor": ""}
            if path.endswith("/markets/orderbooks"):
                obs = load("k_orderbooks_nba.json")["orderbooks"] + load("k_orderbooks_ligamx.json")["orderbooks"]
                want = set(q.get("tickers", []))
                return {"orderbooks": [o for o in obs if o["ticker"] in want]}
            if "/series/" in path:
                t = path.rsplit("/", 1)[-1]
                f = FIX / f"k_series_{t}.json"
                return json.loads(f.read_text()) if f.exists() else None
        if "the-odds-api.com" in url and "/odds" in path:
            data = load("oddsapi_odds_synthetic.json")
            sport = path.split("/sports/")[1].split("/")[0]
            return [e for e in data if e["sport_key"] == sport]
        return None


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def now():
    return NOW
