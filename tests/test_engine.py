from datetime import timedelta

import pytest

from clv_engine.alerts import AlertManager
from clv_engine.config import Settings
from clv_engine.engine import Engine
from clv_engine.ledger import Ledger
from clv_engine.sharp import make_sharp_source
from clv_engine.sharp.base import NoSharpSource
from clv_engine.venues.kalshi import KalshiAdapter
from clv_engine.venues.polymarket import PolymarketAdapter


def settings(**kw):
    s = Settings(db_path=":memory:", leagues=["ligamx", "nba"], alert_sinks=[], snapshot_min_seconds=0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def make(http, s, sharp=None, ledger=None):
    ledger = ledger or Ledger(":memory:")
    sharp = sharp or make_sharp_source(s, http)
    return Engine(s, ledger, PolymarketAdapter(http), KalshiAdapter(http), sharp, AlertManager([], ledger, 1800))


def test_no_key_runs_and_marks_clv_pending(http, now):
    s = settings(sharp_source="theoddsapi", odds_api_key="", min_edge=-0.05)  # force entries; venues agree on the fixtures
    eng = make(http, s)
    assert isinstance(eng.sharp, NoSharpSource) and "ODDS_API_KEY" in eng.sharp.reason
    st = eng.cycle(now)
    assert st.games >= 3 and st.errors == 0 and st.matched_cross >= 3
    assert not any("the-odds-api" in u for u, _ in http.calls)
    rows = eng.ledger.entries()
    assert rows, "consensus reference should still produce paper entries"
    assert {r["ref_source"] for r in rows} == {"consensus"}
    assert all(r["sharp_fair_entry"] is None for r in rows)
    # after kickoff: no Pinnacle close -> pending_sharp; venue CLV still computed
    eng.finalize_clv(now + timedelta(days=3))
    after = eng.ledger.entries()
    assert {r["clv_status"] for r in after} == {"pending_sharp"}
    assert all(r["clv_pct"] is None for r in after)
    assert any(r["venue_clv_pct"] is not None for r in after)


def test_sharp_reference_entry_clv_and_settlement(http, now):
    s = settings(sharp_source="theoddsapi", odds_api_key="test-key", min_edge=0.05, entry_sides=["yes"], soft_edge=0.05)
    eng = make(http, s)
    st = eng.cycle(now)
    assert st.sharp_games == 2 and st.errors == 0
    rows = eng.ledger.entries("venue='polymarket' AND outcome_key='necaxa'")
    assert len(rows) == 1
    e = rows[0]
    assert e["ref_source"] == "pinnacle" and e["side"] == "yes"
    assert e["best_ask"] == pytest.approx(0.14)
    assert e["fee"] == pytest.approx(round(e["shares"] * 0.05 * 0.14 * 0.86, 5), abs=1e-4)
    assert e["edge_entry"] == pytest.approx(e["fair_entry"] / e["effective_price"] - 1, abs=1e-5)
    assert e["edge_entry"] >= 0.05 and e["clv_status"] == "pending_close"
    # soft book: Codere Necaxa 6.5 vs Pinnacle fair ~0.163 -> EV > 5%
    soft = eng.ledger.entries("venue='codere_it'")
    assert soft and all(r["ref_source"] == "pinnacle" for r in soft)
    # second cycle does not duplicate entries
    eng.cycle(now + timedelta(minutes=2))
    assert len(eng.ledger.entries("venue='polymarket' AND outcome_key='necaxa'")) == 1
    # closing captured pre-kickoff; CLV computed after kickoff
    eng.ledger.capture_closing(e["game_key"], "necaxa", "pinnacle", 0.20)
    eng.finalize_clv(now + timedelta(days=4))
    e2 = eng.ledger.entries(f"id={e['id']}")[0]
    assert e2["clv_status"] == "done"
    assert e2["clv_pct"] == pytest.approx(0.20 / e["effective_price"] - 1, abs=1e-5)
    assert e2["clv_pct_raw"] == pytest.approx(0.20 / e["avg_price"] - 1, abs=1e-5)
    # settlement: Necaxa wins
    eng.pm.resolution = lambda slug, ref: 1.0
    eng.kalshi.resolution = lambda ticker: 1.0
    n = eng.settle(now + timedelta(days=4))
    assert n >= 1
    e3 = eng.ledger.entries(f"id={e['id']}")[0]
    assert e3["status"] == "settled" and e3["result_value"] == 1.0
    assert e3["pnl"] == pytest.approx(e["shares"] - e["stake"] - e["fee"], abs=1e-5)


def test_entry_rule_threshold_blocks(http, now):
    s = settings(sharp_source="theoddsapi", odds_api_key="k", min_edge=0.50, soft_entries=False)
    eng = make(http, s)
    eng.cycle(now)
    assert eng.ledger.entries() == []


def test_no_entries_close_to_kickoff(http, now):
    s = settings(min_edge=-1.0, min_minutes_before_start=10_000)
    eng = make(http, s)
    eng.cycle(now)
    assert eng.ledger.entries() == []


def test_alert_dedupe(now):
    from clv_engine.alerts import Alert

    led = Ledger(":memory:")
    sent = []

    class Sink:
        def send(self, a):
            sent.append(a)

    am = AlertManager([Sink()], led, cooldown_seconds=3600)
    assert am.emit(Alert("arb", "k1", "t"))
    assert not am.emit(Alert("arb", "k1", "t again"))
    assert am.emit(Alert("arb", "k2", "t"))
    assert len(sent) == 2


def test_weekly_report_written_once(http, tmp_path):
    from datetime import datetime, timezone

    s = settings(report_dir=str(tmp_path), weekly_report_weekday=0, weekly_report_hour=9)
    eng = make(http, s)
    monday_10_cdmx = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)
    p = eng.maybe_weekly_report(monday_10_cdmx)
    assert p and p.endswith("weekly-2026-10-19.md")
    assert eng.maybe_weekly_report(monday_10_cdmx) is None
    assert eng.maybe_weekly_report(datetime(2026, 10, 20, 16, 0, tzinfo=timezone.utc)) is None


def test_price_band_filters_longshots(http, now):
    s = settings(sharp_source="theoddsapi", odds_api_key="k", min_edge=-1.0, soft_entries=False, min_price=0.2, max_price=0.6)
    eng = make(http, s)
    eng.cycle(now)
    rows = eng.ledger.entries()
    assert rows and all(0.2 <= r["best_ask"] <= 0.6 for r in rows)
