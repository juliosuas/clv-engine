"""Odds API credit scheduler, Pinnacle close capture and the uncaptured-close log. No network."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from clv_engine.alerts import AlertManager
from clv_engine.config import Settings
from clv_engine.engine import Engine
from clv_engine.leagues import LEAGUES
from clv_engine.ledger import Ledger
from clv_engine.sharp import make_sharp_source
from clv_engine.sharp.base import NoSharpSource
from clv_engine.sharp.quota import (
    CLOSE,
    FAR,
    IDLE,
    NEAR,
    credits_per_call,
    estimate_plan,
    format_plan,
    plan_league,
)
from clv_engine.sharp.theoddsapi import TheOddsApiSource
from clv_engine.venues.kalshi import KalshiAdapter
from clv_engine.venues.polymarket import PolymarketAdapter

NBA = LEAGUES["nba"]
KICK = datetime(2026, 10, 20, 23, 30, tzinfo=timezone.utc)  # NBA game in the synthetic Odds API fixture


LMX_KICK = datetime(2026, 10, 21, 0, 7, tzinfo=timezone.utc)  # Liga MX game (Necaxa is mispriced on Polymarket in the fixtures)


def mins(n):
    return timedelta(minutes=n)


def n_odds_calls(http):
    return http.odds_calls


# ---- tiers -----------------------------------------------------------------------------------------------------------------
def test_tiers_by_time_to_kickoff():
    s = Settings()
    now = KICK - mins(10)
    assert plan_league(now, [KICK], [KICK], s)[:2] == (CLOSE, s.sharp_close_poll_seconds)
    # dense polling is for games with pending entries; a known game without entries only gets the near tier
    assert plan_league(now, [KICK], [], s)[:2] == (NEAR, s.sharp_near_poll_seconds)
    s_all = Settings(close_dense_pending_only=False)
    assert plan_league(now, [KICK], [], s_all)[0] == CLOSE
    assert plan_league(KICK - mins(120), [KICK], [], s)[0] == NEAR
    assert plan_league(KICK - mins(400), [KICK], [], s)[:2] == (FAR, s.sharp_poll_seconds)
    assert plan_league(KICK - mins(1500), [KICK], [], s)[:2] == (IDLE, s.sharp_idle_poll_seconds)
    assert plan_league(KICK, [], [], s)[0] == IDLE  # nothing known: discover slowly
    assert plan_league(KICK + mins(5), [KICK], [KICK], s)[0] == IDLE  # kickoff passed: nothing to capture any more
    assert plan_league(KICK - mins(31), [KICK], [KICK], s)[0] == NEAR  # CLOSE_CAPTURE_MINUTES boundary


def test_close_window_is_configurable():
    s = Settings(close_capture_minutes=10)
    assert plan_league(KICK - mins(11), [KICK], [KICK], s)[0] == NEAR
    assert plan_league(KICK - mins(9), [KICK], [KICK], s)[0] == CLOSE


def test_polling_backs_off_far_from_kickoff_and_densifies_near_it(http):
    s = Settings(odds_api_daily_budget=10_000)
    src = TheOddsApiSource(http, "k", s)
    t = KICK - mins(600)  # far tier: every 900 s
    src.fetch(NBA, t)
    src.fetch(NBA, t + timedelta(seconds=300))
    assert n_odds_calls(http) == 1
    src.fetch(NBA, t + timedelta(seconds=905))
    assert n_odds_calls(http) == 2
    # last 30 minutes with a pending entry: a call every ~120 s
    src.set_pending("nba", [KICK])
    t = KICK - mins(25)
    base = n_odds_calls(http)
    for i in range(0, 25 * 60, 30):  # engine loop faster than the tier, so the tier decides
        src.fetch(NBA, t + timedelta(seconds=i))
    close_calls = n_odds_calls(http) - base
    assert 11 <= close_calls <= 13 and src.tier["nba"] == CLOSE
    # without a pending entry the same minutes use the near tier (600 s)
    http2 = type(http)()
    src2 = TheOddsApiSource(http2, "k", s)
    for i in range(0, 25 * 60, 30):
        src2.fetch(NBA, t + timedelta(seconds=i))
    assert http2.odds_calls <= 3


# ---- budget + headers ---------------------------------------------------------------------------------------------------
def test_daily_budget_is_never_exceeded_and_close_polls_keep_a_reserve(http):
    led = Ledger(":memory:")
    s = Settings(odds_api_daily_budget=10, odds_api_close_reserve=0.3, sharp_poll_seconds=1, sharp_near_poll_seconds=1, sharp_idle_poll_seconds=1,
                 sharp_close_poll_seconds=1, odds_api_min_remaining=0)
    src = TheOddsApiSource(http, "k", s, led)
    t = KICK - mins(600)
    for i in range(200):  # hammer the far tier
        src.fetch(NBA, t + timedelta(seconds=20 * i))
    assert n_odds_calls(http) == 7  # 70% of 10: the rest is reserved for the close window
    assert src.quota.usage(t)["skipped"] > 0
    src.set_pending("nba", [KICK])
    t2 = KICK - mins(20)
    for i in range(200):
        src.fetch(NBA, t2 + timedelta(seconds=5 * i))
    assert n_odds_calls(http) == 10  # close polls may use the reserve, never more than the budget
    assert led.usage("2026-10-20")["credits"] == 10
    # a restart keeps counting: usage is persisted in the ledger
    src_b = TheOddsApiSource(http, "k", s, led)
    src_b.set_pending("nba", [KICK])
    src_b.fetch(NBA, KICK - mins(10))
    assert n_odds_calls(http) == 10


def test_degrades_gracefully_serving_cached_data_when_budget_is_gone(http, caplog):
    s = Settings(odds_api_daily_budget=1, sharp_poll_seconds=1, sharp_near_poll_seconds=1, odds_api_close_reserve=0.0)
    src = TheOddsApiSource(http, "k", s)
    t = KICK - mins(600)
    first = src.fetch(NBA, t)
    assert first
    with caplog.at_level(logging.WARNING):
        again = src.fetch(NBA, t + mins(30))
    assert again == first and n_odds_calls(http) == 1  # cached games, no extra credit
    assert any("poll skipped" in r.message and "budget" in r.message for r in caplog.records)
    assert src.fetched_at("nba") == t  # callers can tell the data is old


def test_response_headers_drive_the_remaining_floor(http):
    s = Settings(odds_api_daily_budget=10_000, odds_api_min_remaining=100, sharp_poll_seconds=1, sharp_near_poll_seconds=1, sharp_close_poll_seconds=1)
    http.odds_headers = {"x-requests-remaining": "60", "x-requests-used": "440", "x-requests-last": "1"}
    src = TheOddsApiSource(http, "k", s)
    t = KICK - mins(600)
    src.fetch(NBA, t)
    assert (src.quota.last_remaining, src.quota.last_used) == (60, 440)
    src.fetch(NBA, t + mins(5))  # below ODDS_API_MIN_REMAINING: non-close polls stop
    assert n_odds_calls(http) == 1
    src.set_pending("nba", [KICK])
    src.fetch(NBA, KICK - mins(10))  # close poll still allowed
    assert n_odds_calls(http) == 2


def test_zero_remaining_blocks_everything_and_x_requests_last_sets_cost(http):
    s = Settings(odds_api_daily_budget=10_000, sharp_close_poll_seconds=1)
    http.odds_headers = {"x-requests-remaining": "0", "x-requests-used": "500", "x-requests-last": "3"}
    led = Ledger(":memory:")
    src = TheOddsApiSource(http, "k", s, led)
    src.set_pending("nba", [KICK])
    t = KICK - mins(10)
    src.fetch(NBA, t)
    assert led.usage("2026-10-20")["credits"] == 3 and led.usage("2026-10-20")["remaining"] == 0  # charged per header, not per estimate
    src.fetch(NBA, t + mins(5))
    assert n_odds_calls(http) == 1  # provider says 0 left: even close polls stop


def test_credit_formula_and_historical_cost_are_budgeted(http):
    assert credits_per_call(5) == 1 and credits_per_call(10) == 1 and credits_per_call(11) == 2 and credits_per_call(5, n_markets=3) == 3
    s = Settings(odds_api_daily_budget=5, odds_api_historical=True)
    src = TheOddsApiSource(http, "k", s)
    assert src.closing(NBA, "evt", KICK, KICK) is None
    assert src.closing_blocked and not [c for c in http.calls if "historical" in c[0]]  # 10x cost > budget: no request made


def test_empty_response_with_x_requests_last_zero_is_free(http):
    http.odds_empty = True
    http.odds_headers = {"x-requests-remaining": "499", "x-requests-last": "0"}
    led = Ledger(":memory:")
    src = TheOddsApiSource(http, "k", Settings(), led)
    src.fetch(NBA, KICK - mins(600))
    assert led.usage("2026-10-20")["credits"] == 0 and led.usage("2026-10-20")["requests"] == 1


def test_plan_estimate_scales_with_leagues_and_cadence():
    s = Settings()
    one, five = estimate_plan(s, 1), estimate_plan(s, 5)
    assert five.credits_per_day == pytest.approx(5 * one.credits_per_day) and five.credits_per_30_days == pytest.approx(30 * five.credits_per_day)
    denser = estimate_plan(Settings(sharp_close_poll_seconds=60, poll_seconds=60), 5)
    assert denser.credits_per_day > five.credits_per_day
    assert estimate_plan(Settings(odds_api_daily_budget=100), 5).over_budget and not estimate_plan(Settings(odds_api_daily_budget=100_000), 5).over_budget
    # soft books beyond 10 bookmakers cost a second region
    assert estimate_plan(Settings(soft_books=[f"b{i}" for i in range(10)]), 1).credits_per_day == pytest.approx(2 * one.credits_per_day)
    # windows per day add up to a day (no phantom minutes)
    sec = sum(minutes_calls for minutes_calls in one.breakdown.values())
    assert sec == pytest.approx(one.calls_per_league_day)
    txt = format_plan(s, 5)
    assert "UNVERIFIED" in txt and "ODDS_API_DAILY_BUDGET" in txt


# ---- engine: close capture, stale data, uncaptured-close logging -------------------------------------------------------------
def settings(**kw):
    s = Settings(db_path=":memory:", leagues=["nba"], alert_sinks=[], snapshot_min_seconds=0, sharp_source="theoddsapi", odds_api_key="k",
                 min_edge=0.02, min_depth_usd=0, excluded_leagues=[], excluded_venues=[], soft_entries=False, entry_sides=["yes"])
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def make(http, s, ledger=None, sharp=None):
    ledger = ledger or Ledger(":memory:")
    sharp = sharp or make_sharp_source(s, http, ledger)
    return Engine(s, ledger, PolymarketAdapter(http), KalshiAdapter(http), sharp, AlertManager([], ledger, 1800))


def test_close_capture_records_the_poll_time_not_the_cycle_time(http):
    s = settings(max_sharp_age_minutes=60, leagues=["ligamx"], min_edge=0.05)
    eng = make(http, s)
    t0 = LMX_KICK - mins(50)
    eng.cycle(t0)  # polls Pinnacle at t0
    eng.cycle(t0 + mins(5))  # near tier polls every 10 min: this cycle serves cached data, so the stored time stays t0
    row = eng.ledger.closing_row(eng.ledger.entries()[0]["game_key"], "necaxa", "pinnacle")
    assert row is not None and row[1] == t0


def test_stale_pinnacle_lines_are_not_used_for_entries_or_closes(http, caplog):
    s = settings(max_sharp_age_minutes=20, leagues=["ligamx"], min_edge=0.05)
    eng = make(http, s)
    t0 = LMX_KICK - mins(300)
    eng.cycle(t0)
    n_entries = len(eng.ledger.entries())
    assert n_entries >= 1
    gk = eng.ledger.entries()[0]["game_key"]
    first_close = eng.ledger.closing_row(gk, "necaxa", "pinnacle")
    s.odds_api_daily_budget = 0  # budget gone: polls are skipped, cache keeps ageing
    s.max_entries_per_outcome = 5
    with caplog.at_level(logging.WARNING):
        eng.cycle(t0 + mins(40))
    assert len(eng.ledger.entries()) == n_entries
    assert eng.ledger.closing_row(gk, "necaxa", "pinnacle") == first_close  # not overwritten with a 40-minute-old price
    assert any("stale" in r.message for r in caplog.records) and any("poll skipped" in r.message for r in caplog.records)


def _entry(led, **kw):
    base = dict(venue="polymarket", league="nba", game_key="g-bos-det", game_label="Celtics vs Pistons", outcome_key="bos",
                outcome_label="Celtics", side="yes", start_time="2026-10-20T23:30:00Z", stake=100, shares=200, avg_price=0.5, fee=1,
                effective_price=0.505, ref_source="pinnacle", fair_entry=0.56, edge_entry=0.1, clv_status="pending_close")
    base.update(kw)
    return led.add_entry(**base)


def test_close_must_be_sampled_near_kickoff_to_count(http):
    led = Ledger(":memory:")
    eng = make(http, settings(close_max_age_minutes=20), led)
    fresh, stale = _entry(led, game_key="g1"), _entry(led, game_key="g2")
    led.capture_closing("g1", "bos", "pinnacle", 0.60, KICK - mins(4))
    led.capture_closing("g2", "bos", "pinnacle", 0.60, KICK - mins(90))
    eng.finalize_clv(KICK + mins(5))
    rows = {r["id"]: r for r in led.entries()}
    assert rows[fresh]["clv_status"] == "done" and rows[fresh]["clv_pct"] == pytest.approx(0.60 / 0.505 - 1, abs=1e-5)
    assert rows[stale]["clv_status"] == "pending_sharp" and rows[stale]["clv_pct"] is None
    eng.s.close_max_age_minutes = 0  # check disabled: any stored close counts (legacy behaviour)
    eng.finalize_clv(KICK + mins(6))
    assert {r["id"]: r["clv_status"] for r in led.entries()}[stale] == "done"


@pytest.mark.parametrize("build,expected", [
    ("nokey", "no Pinnacle feed"),
    ("nomatch", "no Pinnacle line was ever sampled"),
    ("old", "CLOSE_MAX_AGE_MINUTES"),
    ("budget", "credit budget"),
    ("league", "not in LEAGUES"),
])
def test_every_uncaptured_close_is_logged_once_with_a_reason(http, caplog, build, expected):
    led = Ledger(":memory:")
    s = settings()
    sharp = NoSharpSource("no ODDS_API_KEY") if build == "nokey" else None
    eng = make(http, s, led, sharp)
    eid = _entry(led, league="epl" if build == "league" else "nba")
    if build == "old":
        led.capture_closing("g-bos-det", "bos", "pinnacle", 0.6, KICK - mins(200))
    if build == "budget":
        eng.sharp.quota.record_skip(KICK)
        eng.sharp.quota.record_skip(KICK)
    with caplog.at_level(logging.WARNING):
        eng.finalize_clv(KICK + mins(5))
        eng.finalize_clv(KICK + mins(7))
    lines = [r.message for r in caplog.records if "close NOT captured" in r.message]
    assert len(lines) == 1, lines  # once per entry, not once per cycle
    assert f"entry #{eid}" in lines[0] and expected in lines[0] and "Celtics" in lines[0] and "kickoff 2026-10-20T23:30:00Z" in lines[0]
    assert led.entries()[0]["clv_status"] == "pending_sharp"


def test_historical_backfill_tried_once_and_budgeted(http):
    led = Ledger(":memory:")
    s = settings(odds_api_historical=True, odds_api_daily_budget=100)
    eng = make(http, s, led)
    led.upsert_game("g-bos-det", "nba", ["bos", "det"], {}, KICK, "sharp", {"sharp": "evt-nba-bos-det"})
    _entry(led)
    eng.finalize_clv(KICK + mins(5))
    eng.finalize_clv(KICK + mins(9))
    hist = [c for c in http.calls if "historical" in c[0]]
    assert len(hist) == 1  # not re-requested every cycle (each call costs 10x)
    assert led.usage("2026-10-20")["credits"] == 10
    # blocked by budget: no request and retried later when budget exists
    led2 = Ledger(":memory:")
    eng2 = make(http, settings(odds_api_historical=True, odds_api_daily_budget=5), led2)
    led2.upsert_game("g-bos-det", "nba", ["bos", "det"], {}, KICK, "sharp", {"sharp": "evt"})
    _entry(led2)
    eng2.finalize_clv(KICK + mins(5))
    assert len([c for c in http.calls if "historical" in c[0]]) == 1
    eng2.s.odds_api_daily_budget = 100
    eng2.finalize_clv(KICK + mins(9))
    assert len([c for c in http.calls if "historical" in c[0]]) == 2


def test_pending_entries_make_the_engine_poll_densely(http):
    s = settings(max_sharp_age_minutes=60)
    eng = make(http, s)
    led = eng.ledger
    _entry(led, game_key="g-existing")
    start = KICK
    base = http.odds_calls
    for i in range(10):  # 20 minutes of 2-minute cycles inside the close window
        eng.cycle(start - mins(25) + timedelta(minutes=2 * i))
    assert http.odds_calls - base >= 8  # roughly one poll per cycle
    http3 = type(http)()
    eng3 = make(http3, settings(max_sharp_age_minutes=60))
    for i in range(10):
        eng3.cycle(start - mins(25) + timedelta(minutes=2 * i))
    assert http3.odds_calls <= 3  # no pending entry: near tier


def test_quota_command_and_status_do_not_need_network(tmp_path, capsys, monkeypatch):
    from clv_engine.cli import main

    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.sqlite"))
    assert main(["--env-file", "", "quota"]) == 0
    out = capsys.readouterr().out
    assert "credit plan" in out and "today (UTC" in out
