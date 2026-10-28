"""Odds API credit scheduler (monthly budget, kickoff rounds), Pinnacle close capture and the uncaptured-close log. No network."""

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
    ENTRY,
    FINAL,
    build_rounds,
    credits_per_call,
    estimate_plan,
    format_plan,
    next_poll,
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


def drive(src, league, kickoff, start_min, end_min, step_s=120, pending=True):
    """Run the engine-like loop (one fetch per POLL_SECONDS) between two offsets from kickoff."""
    if pending:
        src.set_pending(league.key, [kickoff])
    src.set_schedule(league.key, [kickoff])
    t = kickoff + mins(start_min)
    while t <= kickoff + mins(end_min):
        src.fetch(league, t)
        t += timedelta(seconds=step_s)


# ---- settings / rounds ----------------------------------------------------------------------------------------------------
def test_monthly_budget_defaults():
    s = Settings()
    assert (s.odds_api_monthly_budget, s.odds_api_hard_cap) == (500, 450)
    assert not hasattr(s, "odds_api_daily_budget")


def test_removed_variables_warn_and_are_ignored(monkeypatch, caplog):
    monkeypatch.setenv("ODDS_API_DAILY_BUDGET", "1000")
    with caplog.at_level(logging.WARNING):
        s = Settings.from_env(dotenv=None)
    assert any("ODDS_API_DAILY_BUDGET" in r.message and "ignoring" in r.message.lower() for r in caplog.records)
    assert s.odds_api_hard_cap == 450


def test_kickoffs_within_the_close_window_span_share_one_round():
    s = Settings()
    now = KICK - mins(60)
    ks = [KICK, KICK + mins(3), KICK + mins(4), KICK + mins(30), KICK + mins(200)]
    rounds = build_rounds(now, ks, [KICK + mins(30)], s)
    assert [(r.start, r.end, r.pending) for r in rounds] == [
        (KICK, KICK + mins(4), False), (KICK + mins(30), KICK + mins(30), True), (KICK + mins(200), KICK + mins(200), False)]
    assert build_rounds(KICK + mins(1), [KICK], [], s) == []  # started games are not rounds


def test_next_poll_entry_then_one_final_close_in_the_window():
    s = Settings()  # poll_seconds=120, window T-5..T-1, entry lead 25, one close poll per round
    rounds = build_rounds(KICK - mins(60), [KICK], [KICK], s)
    assert next_poll(KICK - mins(60), rounds, None, [], s) is None  # nothing before the entry lead
    due = next_poll(KICK - mins(24), rounds, None, [], s)
    assert due and due.kind == ENTRY
    assert next_poll(KICK - mins(24), rounds, KICK - mins(24), [KICK - mins(24)], s) is None  # fresh data: no second entry poll
    assert next_poll(KICK - mins(4), rounds, KICK - mins(24), [], s) is None  # not the last cycle of the window yet
    due = next_poll(KICK - mins(2.5), rounds, KICK - mins(24), [], s)
    assert due and due.kind == FINAL
    assert next_poll(KICK - mins(2.5), rounds, KICK - mins(2.5), [KICK - mins(2.5)], s) is None  # one close poll per round is enough
    assert next_poll(KICK + mins(1), build_rounds(KICK + mins(1), [KICK], [KICK], s), None, [], s) is None  # started


def test_no_final_poll_without_pending_entries_and_early_close_is_optional():
    s = Settings()
    rounds = build_rounds(KICK - mins(5), [KICK], [], s)
    assert next_poll(KICK - mins(2), rounds, KICK - mins(5), [], s) is None  # no entry waits for this close
    s2 = Settings(close_polls_per_round=2)
    pend = build_rounds(KICK - mins(5), [KICK], [KICK], s2)
    assert next_poll(KICK - mins(4.9), pend, KICK - mins(24), [], s2).kind == "close_early"
    assert next_poll(KICK - mins(2), pend, KICK - mins(24), [KICK - mins(4.9)], s2).kind == FINAL


def test_one_poll_per_sport_and_round_two_games_one_credit(http):
    """Several kickoffs of a sport inside one round share a single /odds poll (1 credit), entry + close = 2 per round."""
    led = Ledger(":memory:")
    src = TheOddsApiSource(http, "k", Settings(), led)
    k2 = KICK + mins(3)
    src.set_pending("nba", [KICK, k2])
    src.set_schedule("nba", [KICK, k2])
    t = KICK - mins(30)
    while t < KICK + mins(2):
        src.fetch(NBA, t)
        t += mins(2)
    assert n_odds_calls(http) == 2 and led.usage("2026-10-20")["credits"] == 2
    odds = [c for c in http.calls if "/odds" in c[0]]
    params = odds[0][1]
    assert params["bookmakers"] == "pinnacle" and params["markets"] == "h2h" and params["regions"] == "eu"


def test_nothing_is_polled_outside_the_slots_and_no_historical_by_default(http):
    src = TheOddsApiSource(http, "k", Settings(), Ledger(":memory:"))
    src.set_schedule("nba", [KICK])
    for i in range(0, 24 * 60, 10):
        src.fetch(NBA, KICK - mins(24 * 60) + mins(i))
    assert n_odds_calls(http) == 1  # only the entry poll of the one known round
    assert not [c for c in http.calls if "historical" in c[0]]


# ---- budget + headers ---------------------------------------------------------------------------------------------------
def test_monthly_hard_cap_blocks_even_final_closes_and_records_the_lost_close(http, caplog):
    led = Ledger(":memory:")
    s = Settings(odds_api_hard_cap=3)
    src = TheOddsApiSource(http, "k", s, led)
    for _ in range(3):
        src.quota.record_call(KICK - mins(60), {})
    assert src.quota.spendable(KICK) == 0
    src.set_pending("nba", [KICK])
    src.set_schedule("nba", [KICK])
    with caplog.at_level(logging.WARNING):
        drive(src, NBA, KICK, -30, 0)
    assert n_odds_calls(http) == 0
    warn = [r.message for r in caplog.records if "close poll NOT made" in r.message]
    assert len(warn) == 1 and "hard cap" in warn[0] and "no_close" in warn[0]  # one WARNING per round, not per cycle
    miss = led.missed_close_for("nba", KICK)
    assert miss and "hard cap" in miss["reason"]


def test_final_close_is_made_when_entry_polls_are_throttled(http):
    """Entry polls obey the daily pace and the credits owed to final closes; the final close of a pending round never does."""
    led = Ledger(":memory:")
    s = Settings(odds_api_hard_cap=2, odds_api_daily_burst=1.0)
    src = TheOddsApiSource(http, "k", s, led)
    drive(src, NBA, KICK, -30, 0)  # a pending entry exists: its close (1 credit of the 2) is reserved, the entry poll is refused
    assert n_odds_calls(http) == 1 and led.missed_closes() == []
    assert src.fetched_at("nba") is not None and src.fetched_at("nba") >= KICK - mins(5)


def test_headers_are_the_truth_for_used_and_remaining(http):
    s = Settings()
    http.odds_headers = {"x-requests-remaining": "430", "x-requests-used": "70", "x-requests-last": "1"}
    led = Ledger(":memory:")
    src = TheOddsApiSource(http, "k", s, led)
    t = KICK - mins(24)
    src.set_schedule("nba", [KICK])
    src.fetch(NBA, t)
    assert (src.quota.last_remaining, src.quota.last_used) == (430, 70)
    assert src.quota.used_this_month(t) == 70  # not the local count of 1 credit
    assert src.quota.spendable(t) == 380  # hard cap 450 - 70 used (provider remaining is 430)
    assert led.month_usage("2026-10")["remaining"] == 430
    # a restart (new tracker, same ledger) starts from the persisted header values
    src2 = TheOddsApiSource(http, "k", s, led)
    assert src2.quota.used_this_month(t) == 70 and src2.quota.remaining_now(t) == 430


def test_provider_remaining_below_the_cost_stops_even_final_closes_and_last_header_sets_cost(http):
    s = Settings()
    http.odds_headers = {"x-requests-remaining": "0", "x-requests-used": "500", "x-requests-last": "3"}
    led = Ledger(":memory:")
    src = TheOddsApiSource(http, "k", s, led)
    drive(src, NBA, KICK, -4, -2)
    assert led.usage("2026-10-20")["credits"] == 3 and led.usage("2026-10-20")["remaining"] == 0  # charged per header, not per estimate
    n = n_odds_calls(http)
    src.set_pending("nba", [KICK + mins(60)])
    src.set_schedule("nba", [KICK + mins(60)])
    for i in range(5):
        src.fetch(NBA, KICK + mins(56 + i))
    assert n_odds_calls(http) == n  # provider says 0 left


def test_calendar_month_rollover_ignores_last_months_headers(http):
    led = Ledger(":memory:")
    s = Settings()
    src = TheOddsApiSource(http, "k", s, led)
    src.quota.record_call(datetime(2026, 10, 30, 12, tzinfo=timezone.utc), {"x-requests-remaining": "5", "x-requests-used": "495", "x-requests-last": "1"})
    nov = datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc)
    assert src.quota.spendable(datetime(2026, 10, 30, 13, tzinfo=timezone.utc)) == 0  # October: 5 left at the provider
    assert src.quota.remaining_now(nov) is None and src.quota.used_this_month(nov) == 0
    assert src.quota.spendable(nov) == 450  # a new month: the local count restarts
    assert src.quota.days_left(nov) == 30 and src.quota.days_left(datetime(2026, 11, 30, tzinfo=timezone.utc)) == 1
    # the daily allowance spreads what is spendable over the days left, capped by the burst
    assert src.quota.daily_allowance(nov) == pytest.approx(min(450 / 30, 3.0 * 450 / 30))


def test_degrades_gracefully_serving_cached_data_when_budget_is_gone(http, caplog):
    led = Ledger(":memory:")
    s = Settings()
    src = TheOddsApiSource(http, "k", s, led)
    src.set_schedule("nba", [KICK])
    first = src.fetch(NBA, KICK - mins(24))
    assert first and n_odds_calls(http) == 1
    s.odds_api_hard_cap = 1  # budget gone before the close poll
    src.set_pending("nba", [KICK])
    with caplog.at_level(logging.WARNING):
        again = src.fetch(NBA, KICK - mins(2))
    assert again == first and n_odds_calls(http) == 1  # cached games, no extra credit
    assert any("close poll NOT made" in r.message for r in caplog.records)
    assert src.fetched_at("nba") == KICK - mins(24)  # callers can tell the data is old


def test_credit_formula_and_historical_cost_are_budgeted(http):
    assert credits_per_call(5) == 1 and credits_per_call(10) == 1 and credits_per_call(11) == 2 and credits_per_call(5, n_markets=3) == 3
    s = Settings(odds_api_hard_cap=5, odds_api_historical=True)
    src = TheOddsApiSource(http, "k", s)
    assert src.closing(NBA, "evt", KICK, KICK) is None
    assert src.closing_blocked and not [c for c in http.calls if "historical" in c[0]]  # 10x cost > cap: no request made


def test_empty_response_with_x_requests_last_zero_is_free(http):
    http.odds_empty = True
    http.odds_headers = {"x-requests-remaining": "499", "x-requests-last": "0"}
    led = Ledger(":memory:")
    src = TheOddsApiSource(http, "k", Settings(), led)
    src.set_schedule("nba", [KICK])
    src.fetch(NBA, KICK - mins(24))
    assert led.usage("2026-10-20")["credits"] == 0 and led.usage("2026-10-20")["requests"] == 1


def test_plan_estimate_scales_with_leagues_and_cadence():
    s = Settings()
    one, five = estimate_plan(s, 1), estimate_plan(s, 5)
    assert five.credits_per_day == pytest.approx(5 * one.credits_per_day) and five.credits_per_30_days == pytest.approx(30 * five.credits_per_day)
    assert one.credits_per_round == 2  # one entry poll + one close poll per round
    assert estimate_plan(Settings(close_polls_per_round=2), 1).credits_per_round == 3
    assert five.over_budget  # 5 leagues x 2 rounds x 2 credits x 30 days = 600 > 450
    assert not estimate_plan(s, 2).over_budget  # 240 <= 450
    assert estimate_plan(Settings(soft_books=[f"b{i}" for i in range(10)], soft_entries=True), 1, n_books=11).credits_per_day == pytest.approx(2 * one.credits_per_day)
    txt = format_plan(s, 5)
    assert "ODDS_API_HARD_CAP=450" in txt and "ODDS_API_MONTHLY_BUDGET=500" in txt and "OVER" in txt


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
    t0 = LMX_KICK - mins(24)
    eng.cycle(t0)  # the round's entry poll (T-25 lead) fetches Pinnacle at t0
    eng.cycle(t0 + mins(5))  # no poll is due before the close window: this cycle serves cached data, so the stored time stays t0
    row = eng.ledger.closing_row(eng.ledger.entries()[0]["game_key"], "necaxa", "pinnacle")
    assert row is not None and row[1] == t0


def test_stale_pinnacle_lines_are_not_used_for_entries_or_closes(http, caplog):
    s = settings(max_sharp_age_minutes=20, leagues=["ligamx"], min_edge=0.05)
    eng = make(http, s)
    t0 = LMX_KICK - mins(24)
    eng.cycle(t0)
    n_entries = len(eng.ledger.entries())
    assert n_entries >= 1
    gk = eng.ledger.entries()[0]["game_key"]
    first_close = eng.ledger.closing_row(gk, "necaxa", "pinnacle")
    s.odds_api_hard_cap = 0  # budget gone: polls are skipped, cache keeps ageing
    s.max_entries_per_outcome = 5
    with caplog.at_level(logging.WARNING):
        eng.cycle(t0 + mins(22))  # still before kickoff (T-2), but the last Pinnacle sample is 22 minutes old
    assert len(eng.ledger.entries()) == n_entries
    assert eng.ledger.closing_row(gk, "necaxa", "pinnacle") == first_close  # not overwritten with a 40-minute-old price
    assert any("stale" in r.message for r in caplog.records) and any("close poll NOT made" in r.message for r in caplog.records)


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
    ("lostround", "close poll for the"),
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
    if build == "lostround":
        led.add_missed_close("nba", KICK, KICK, KICK - mins(2), "monthly hard cap ODDS_API_HARD_CAP=450 reached")
    with caplog.at_level(logging.WARNING):
        eng.finalize_clv(KICK + mins(5))
        eng.finalize_clv(KICK + mins(7))
    lines = [r.message for r in caplog.records if "close NOT captured" in r.message]
    assert len(lines) == 1, lines  # once per entry, not once per cycle
    assert f"entry #{eid}" in lines[0] and expected in lines[0] and "Celtics" in lines[0] and "kickoff 2026-10-20T23:30:00Z" in lines[0]
    assert led.entries()[0]["clv_status"] == ("no_close" if build == "lostround" else "pending_sharp")


def test_historical_backfill_tried_once_and_budgeted(http):
    led = Ledger(":memory:")
    s = settings(odds_api_historical=True)
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
    eng2 = make(http, settings(odds_api_historical=True, odds_api_hard_cap=5), led2)
    led2.upsert_game("g-bos-det", "nba", ["bos", "det"], {}, KICK, "sharp", {"sharp": "evt"})
    _entry(led2)
    eng2.finalize_clv(KICK + mins(5))
    assert len([c for c in http.calls if "historical" in c[0]]) == 1
    eng2.s.odds_api_hard_cap = 450
    eng2.finalize_clv(KICK + mins(9))
    assert len([c for c in http.calls if "historical" in c[0]]) == 2


def test_engine_polls_once_per_round_for_entry_and_once_for_the_close(http):
    s = settings(max_sharp_age_minutes=60)
    eng = make(http, s)
    _entry(eng.ledger)  # a pending entry for the 23:30 game (Pinnacle commence_time); the venues list it at 23:00 in the fixtures
    for i in range(31):  # 2-minute cycles from T-60 to T (T = Pinnacle kickoff)
        eng.cycle(KICK - mins(60) + timedelta(minutes=2 * i))
    # entry poll at the venue round (23:00 - 25 min), a second entry poll for the Pinnacle-dated round (23:30 - 25 min, the first sample is 30 min
    # old by then), and ONE final close poll inside T-5..T-1: never one poll per cycle
    assert http.odds_calls == 3
    assert eng.ledger.usage("2026-10-20")["credits"] == 3
    closes = [r for r in eng.ledger.conn.execute("SELECT captured_at FROM closing").fetchall()]
    assert closes


def test_quota_command_and_status_do_not_need_network(tmp_path, capsys, monkeypatch):
    from clv_engine.cli import main

    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.sqlite"))
    assert main(["--env-file", "", "quota"]) == 0
    out = capsys.readouterr().out
    assert "credit plan" in out and "today (UTC" in out and "this month (UTC" in out and "hard cap 450 of 500" in out
