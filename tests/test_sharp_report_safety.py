import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from clv_engine.config import Settings
from clv_engine.http import _redact
from clv_engine.leagues import LEAGUES
from clv_engine.ledger import Ledger
from clv_engine.report import format_summary, summary, weekly
from clv_engine.sharp.theoddsapi import TheOddsApiSource, parse_events

from .conftest import load

PKG = Path(__file__).resolve().parents[1] / "clv_engine"


def test_parse_odds_api_three_way_and_devig():
    games = parse_events(load("oddsapi_odds_synthetic.json")[1:], LEAGUES["ligamx"], "power", "pinnacle", ["codere_it"])
    g = games[0]
    assert (g.home, g.away) == ("guadalajara", "necaxa")
    assert set(g.sharp.fair) == {"guadalajara", "necaxa", "draw"}
    assert sum(g.sharp.fair.values()) == pytest.approx(1.0)
    assert "codere_it" in g.soft
    assert g.start.isoformat().startswith("2026-10-21T00:07")


def test_odds_api_caches_between_polls(http, now):
    s = Settings(entry_poll_lead_minutes=100_000)
    src = TheOddsApiSource(http, "secret", s)
    src.set_schedule("nba", [now + timedelta(hours=30)])
    src.fetch(LEAGUES["nba"], now)
    src.fetch(LEAGUES["nba"], now)
    assert sum(1 for u, _ in http.calls if "the-odds-api" in u) == 1


def test_api_key_redacted():
    assert "secret" not in _redact("https://api.the-odds-api.com/v4/sports/x/odds?apiKey=secret&markets=h2h")


def test_settings_repr_hides_secrets(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "supersecret")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/abc")
    s = Settings.from_env(dotenv=None)
    assert s.odds_api_key == "supersecret"
    assert "supersecret" not in repr(s) and "hooks.example" not in repr(s)


def test_reports_render():
    led = Ledger(":memory:")
    base = dict(venue="polymarket", league="ligamx", game_key="g", game_label="A vs B", outcome_key="a", outcome_label="A", side="yes",
                start_time="2026-10-20T00:00:00Z", stake=50.0, shares=100.0, avg_price=0.5, fee=1.25, effective_price=0.5125,
                ref_source="pinnacle", fair_entry=0.55, edge_entry=0.073)
    led.add_entry(**base, clv_pct=0.05, clv_status="done", status="settled", result_value=1.0, pnl=48.75, settled_at="2026-10-21T00:00:00Z")
    led.add_entry(**{**base, "venue": "kalshi", "league": "nba"}, clv_status="pending_sharp")
    s = summary(led)
    assert s["overall"]["entries"] == 2 and s["overall"]["settled"] == 1
    assert s["overall"]["hit_rate"] == 1.0 and s["overall"]["pnl"] == pytest.approx(48.75)
    assert s["by_league"]["nba"]["pending_sharp"] == 1
    txt = format_summary(s)
    assert "PAPER LEDGER" in txt and "ligamx" in txt
    md = weekly(led)
    assert "weekly report" in md and "Paper only" in md and "| ligamx |" in md


def test_no_trading_or_wallet_code():
    """Static guard: the package must not reference order/wallet endpoints or signing."""
    forbidden = re.compile(r"(/order\b|/orders\b|post_order|create_order|private_key|eth_account|py_clob_client|signTypedData|/portfolio)", re.I)
    for f in PKG.rglob("*.py"):
        text = f.read_text()
        hits = [m.group(0) for m in forbidden.finditer(text)]
        assert not hits, f"{f.name}: {hits}"


def test_http_is_get_only():
    src = (PKG / "http.py").read_text()
    assert "method=" not in src and "data=" not in src
