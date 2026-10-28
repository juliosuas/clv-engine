"""status / graduation: measured sample, CI, projection and the numeric gate. No network, no trading code."""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clv_engine.cli import main
from clv_engine.config import Settings
from clv_engine.graduation import (
    PROPOSED_RISK_LIMITS,
    cluster_bootstrap,
    format_graduation,
    format_status,
    graduation,
    measured_sample,
    status,
    t_interval,
    t_quantile,
)
from clv_engine.ledger import Ledger
from clv_engine.report import weekly

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 20, tzinfo=timezone.utc)


def cfg(**kw):
    s = Settings(min_edge=0.05, excluded_leagues=["ucl"], excluded_venues=[], grad_bootstrap_samples=800)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def add(led, i, *, clv=0.08, league="nba", venue="polymarket", settled=True, won=True, ref="pinnacle", status_="done", partial=0, edge=0.08,
        day=None, game=None, close=True):
    eff = 0.50
    created = (day or (T0 + timedelta(hours=i * 2))).strftime("%Y-%m-%dT%H:%M:%SZ")
    eid = led.add_entry(
        created_at=created, venue=venue, league=league, game_key=game or f"g{i}", game_label=f"game {i}", outcome_key="a", outcome_label="A", side="yes",
        start_time=created, stake=100.0, shares=200.0, avg_price=0.49, fee=1.0, effective_price=eff, partial=partial, ref_source=ref,
        fair_entry=0.54, edge_entry=edge, sharp_fair_close=(eff * (1 + clv)) if close else None,
        clv_pct=clv if status_ == "done" else None, clv_status=status_)
    if settled:
        led.update_entry(eid, status="settled", result_value=1.0 if won else 0.0, pnl=(100.0 if won else -101.0), settled_at=created)
    return eid


def fill(led, n, **kw):
    rng = random.Random(7)
    for i in range(n):
        add(led, i, clv=0.06 + rng.uniform(-0.02, 0.02), league=["nba", "epl"][i % 2], venue=["polymarket", "kalshi"][(i // 2) % 2],
            won=(i % 3 != 0), **kw)


# ---- statistics -----------------------------------------------------------------------------------------------------------------
def test_t_quantiles_match_tables():
    assert t_quantile(0.95, 29) == pytest.approx(1.699, abs=2e-3)
    assert t_quantile(0.95, 10) == pytest.approx(1.812, abs=3e-3)
    assert t_quantile(0.975, 299) == pytest.approx(1.968, abs=2e-3)


def test_t_interval_and_bootstrap_basic_properties():
    xs = [0.05, 0.07, 0.04, 0.09, 0.06, 0.08, 0.05, 0.07]
    lo, hi = t_interval(xs, 0.90)
    assert lo < sum(xs) / len(xs) < hi and lo > 0
    assert t_interval([0.1, 0.2], 0.9) is None  # too few
    groups = [[x] for x in xs]
    blo, bhi = cluster_bootstrap(groups, 0.90, 800)
    assert blo < sum(xs) / len(xs) < bhi
    assert cluster_bootstrap(groups, 0.90, 800) == (blo, bhi)  # deterministic seed
    assert cluster_bootstrap([[0.1]], 0.9, 100) is None


def test_bootstrap_is_wider_when_entries_of_one_game_are_duplicated():
    xs = [0.06, -0.02, 0.09, 0.01, 0.07, -0.01, 0.05, 0.03]
    indep = cluster_bootstrap([[x] for x in xs], 0.90, 1500)
    clustered = cluster_bootstrap([[x, x, x, x] for x in xs], 0.90, 1500)
    t_dup = t_interval([x for x in xs for _ in range(4)], 0.90)
    assert clustered[1] - clustered[0] == pytest.approx(indep[1] - indep[0], rel=0.25)  # copies add no information
    assert t_dup[1] - t_dup[0] < indep[1] - indep[0]  # the naive t interval pretends they do


# ---- sample + status ----------------------------------------------------------------------------------------------------------
def test_measured_sample_rules():
    led = Ledger(":memory:")
    good = add(led, 1)
    add(led, 2, ref="consensus")                       # not priced vs Pinnacle
    add(led, 3, status_="pending_sharp", close=False)  # no Pinnacle close
    add(led, 4, partial=1)                             # partial fill
    add(led, 5, edge=0.03)                             # below MIN_EDGE
    add(led, 6, league="ucl")                          # excluded league
    add(led, 7, venue="codere_it")                     # soft book
    k = add(led, 8, venue="kalshi")
    assert [r["id"] for r in measured_sample(led, cfg())] == [good, k]
    assert [r["id"] for r in measured_sample(led, cfg(excluded_venues=["kalshi"]))] == [good]
    # lowering MIN_EDGE widens the sample; existing rows were never modified
    assert len(measured_sample(led, cfg(min_edge=0.02))) == 3


def test_status_empty_db_is_insufficient_data():
    st = status(Ledger(":memory:"), cfg(), NOW)
    assert st["sample_n"] == 0 and st["mean_clv"] is None and st["ci"]["bootstrap"] is None and st["entries_per_day"] is None
    assert st["days_to_target"] is None and "insufficient data" in st["projection"]
    txt = format_status(st)
    assert "n/a" in txt and "insufficient data" in txt and "Odds API credits today" in txt


def test_status_rate_needs_enough_entries_and_days():
    led = Ledger(":memory:")
    for i in range(12):
        add(led, i, day=NOW - timedelta(hours=1) + timedelta(minutes=i))  # 12 entries, all within one hour
    st = status(led, cfg(), NOW)
    assert st["entries_per_day"] is None and "insufficient data" in st["projection"]
    led2 = Ledger(":memory:")
    for i in range(5):
        add(led2, i, day=NOW - timedelta(days=10) + timedelta(days=i))  # 5 entries over 10 days: too few
    assert status(led2, cfg(), NOW)["days_to_target"] is None


def test_status_projects_days_to_300_from_the_observed_rate():
    led = Ledger(":memory:")
    for i in range(30):  # 30 measured entries over 10 days = 3/day
        add(led, i, day=NOW - timedelta(days=10) + timedelta(hours=8 * i), settled=i < 20)
    add(led, 99, ref="consensus")  # unmeasured entries count in totals only
    st = status(led, cfg(), NOW)
    assert st["sample_n"] == 30 and st["sample_settled"] == 20 and st["entries_total"] == 31 and st["entries_with_pinnacle_close"] == 31
    assert st["settled_total"] == 21
    assert st["entries_per_day"] == pytest.approx(3.0)
    assert st["days_to_target"] == pytest.approx(270 / st["entries_per_day"])
    assert st["mean_clv"] == pytest.approx(0.08) and st["ci"]["t"][0] == pytest.approx(0.08)  # zero variance: degenerate CI
    assert "naive" in st["projection"]
    json.loads(json.dumps(st, default=str))


def test_status_reports_target_reached():
    led = Ledger(":memory:")
    for i in range(6):
        add(led, i, day=NOW - timedelta(days=5) + timedelta(hours=i))
    st = status(led, cfg(grad_min_games=5), NOW)
    assert st["days_to_target"] == 0.0 and "reached" in st["projection"]


# ---- graduation gate --------------------------------------------------------------------------------------------------------
def crit(g, prefix):
    return next(c for c in g["criteria"] if c["name"].startswith(prefix))


def test_gate_not_ready_on_empty_and_never_claims_authorization():
    g = graduation(Ledger(":memory:"), cfg(), NOW)
    assert not g["passed"] and g["met"] == 0 and g["state"] == "extend" and "EXTEND" in g["verdict"]
    assert all(c["ok"] in (None, False) for c in g["criteria"])  # unknown counts as not met


def test_gate_passes_only_with_all_criteria():
    led = Ledger(":memory:")
    fill(led, 300)
    g = graduation(led, cfg(), NOW)
    assert g["sample_n"] == 300 and g["passed"], format_graduation(g)
    assert "not an authorization" in g["verdict"] and "nothing in this repository places real orders" in g["verdict"]
    assert PROPOSED_RISK_LIMITS["max_per_ticket"].startswith("1%")


def test_gate_below_300_games_is_extend_not_fail():
    led = Ledger(":memory:")
    fill(led, 299)
    g = graduation(led, cfg(), NOW)
    assert not g["passed"] and crit(g, "sample size")["ok"] is None and g["state"] == "extend"  # insufficient, not failed


def test_gate_requires_ci_lower_bound_above_zero():
    led = Ledger(":memory:")
    rng = random.Random(3)
    for i in range(300):  # mean ~ +0.2% with sd 12%: positive mean, interval straddles 0
        add(led, i, clv=0.002 + rng.uniform(-0.2, 0.2), league=["nba", "epl"][i % 2], venue=["polymarket", "kalshi"][(i // 2) % 2])
    g = graduation(led, cfg(), NOW)
    assert crit(g, "mean CLV")["ok"] is True and crit(g, "90% CI")["ok"] is False and g["state"] == "fail"


def test_gate_requires_negative_or_zero_mean_to_fail():
    led = Ledger(":memory:")
    for i in range(300):
        add(led, i, clv=-0.03 if i % 2 else -0.01, league=["nba", "epl"][i % 2], venue=["polymarket", "kalshi"][(i // 2) % 2])
    g = graduation(led, cfg(), NOW)
    assert crit(g, "mean CLV")["ok"] is False and g["state"] == "fail"


def test_gate_requires_positive_after_fee_pnl_on_the_same_sample():
    led = Ledger(":memory:")
    fill(led, 300, settled=False)
    g = graduation(led, cfg(), NOW)
    assert crit(g, "after-fee")["ok"] is None and g["state"] == "extend"  # not enough settled: wait, neither pass nor fail
    led2 = Ledger(":memory:")
    for i in range(300):  # good CLV but results went against it: losing paper P&L
        add(led2, i, clv=0.06, league=["nba", "epl"][i % 2], venue=["polymarket", "kalshi"][(i // 2) % 2], won=(i % 10 == 0))
    g2 = graduation(led2, cfg(), NOW)
    assert crit(g2, "after-fee")["ok"] is False and g2["state"] == "fail"


def test_gate_fails_when_one_league_drives_the_result():
    led = Ledger(":memory:")
    for i in range(300):
        nba = i % 10 < 8  # 80% NBA
        add(led, i, league="nba" if nba else "epl", clv=0.08 if nba else -0.04, venue=["polymarket", "kalshi"][(i // 2) % 2])
    g = graduation(led, cfg(), NOW)
    assert crit(g, "no single league")["ok"] is False and not g["passed"]
    led2 = Ledger(":memory:")  # all one league: also fails
    for i in range(300):
        add(led2, i, league="nba", venue=["polymarket", "kalshi"][(i // 2) % 2])
    assert crit(graduation(led2, cfg(), NOW), "no single league")["ok"] is False


def test_gate_fails_when_one_venue_drives_the_result_or_only_one_venue_exists():
    led = Ledger(":memory:")
    for i in range(300):
        pm = i % 2 == 0
        add(led, i, venue="polymarket" if pm else "kalshi", clv=0.10 if pm else -0.001, league=["nba", "epl"][(i // 2) % 2])
    g = graduation(led, cfg(), NOW)
    assert crit(g, "no single venue")["ok"] is False
    led2 = Ledger(":memory:")
    fill(led2, 300)
    s = cfg(excluded_venues=["kalshi"])  # the default config with Kalshi off: sample shrinks and one venue cannot clear the gate
    assert not graduation(led2, s, NOW)["passed"]
    s.grad_min_venues, s.grad_min_games = 1, 100
    assert crit(graduation(led2, s, NOW), "no single venue")["ok"] is True


def test_default_settings_are_the_proposed_thresholds():
    s = Settings()
    assert (s.grad_min_games, s.grad_ci_level, s.min_edge) == (300, 0.90, 0.05)
    assert s.excluded_leagues == ["ucl"] and s.excluded_venues == ["kalshi"]


# ---- CLI / report / safety -------------------------------------------------------------------------------------------------
def test_cli_status_and_graduation(tmp_path, capsys, monkeypatch):
    db = tmp_path / "t.sqlite"
    led = Ledger(str(db))
    fill(led, 40)
    led.conn.close()
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("EXCLUDED_VENUES", "")
    assert main(["--env-file", "", "status"]) == 0
    out = capsys.readouterr().out
    assert "CLV STATUS" in out and "mean CLV" in out
    assert main(["--env-file", "", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sample_n"] == 40
    assert main(["--env-file", "", "graduation"]) == 0
    out = capsys.readouterr().out
    assert "GRADUATION CHECK" in out and "[N/A ] sample size" in out and "EXTEND" in out and "not enforced anywhere" in out
    assert main(["--env-file", "", "graduation", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_weekly_report_includes_status_and_graduation_when_settings_given():
    led = Ledger(":memory:")
    fill(led, 12)
    assert "GRADUATION CHECK" not in weekly(led, NOW)
    txt = weekly(led, NOW, settings=cfg())
    assert "CLV STATUS" in txt and "GRADUATION CHECK" in txt


def test_graduation_doc_exists_and_labels_limits_as_proposals():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "GRADUATION.md").read_text()
    for needle in ("300", "90%", "1%", "kill switch", "proposal"):
        assert needle.lower() in doc.lower(), needle
    assert "nothing in this repository places real orders" in doc.lower()


# ---- adjustments: game clustering, pre-registered rules, extend vs fail, retroactive rate, deadline ----------------------------
def test_n_measured_counts_distinct_games_and_ci_is_per_game():
    led = Ledger(":memory:")
    for i in range(10):  # 10 games x 4 correlated entries each
        for j in range(4):
            add(led, i * 10 + j, game=f"g{i}", clv=0.05 + 0.01 * (i % 3), venue=["polymarket", "kalshi"][j % 2], day=T0 + timedelta(hours=i))
    st = status(led, cfg(), NOW)
    assert st["sample_n"] == 40 and st["games_measured"] == 10 and st["ci"]["n_games"] == 10
    flat = Ledger(":memory:")  # same CLVs but 40 independent games: the interval over games is wider for the clustered ledger
    for i in range(40):
        add(flat, i, game=f"h{i}", clv=0.05 + 0.01 * ((i // 4) % 3), day=T0 + timedelta(hours=i))
    wide = status(led, cfg(), NOW)["ci"]["t"]
    narrow = status(flat, cfg(), NOW)["ci"]["t"]
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])
    g = graduation(led, cfg(grad_min_games=40), NOW)  # 40 entries but only 10 games: size criterion not met
    assert crit(g, "sample size")["ok"] is None and g["state"] == "extend"


def test_game_level_mean_not_entry_weighted():
    led = Ledger(":memory:")
    for j in range(9):
        add(led, j, game="big", clv=0.00, day=T0 + timedelta(hours=j))  # 9 entries of one game at 0%
    add(led, 50, game="small", clv=0.10, day=T0 + timedelta(hours=20))
    st = status(led, cfg(), NOW)
    assert st["mean_clv"] == pytest.approx(0.05) and st["mean_clv_entry_weighted"] == pytest.approx(0.01)


def test_extend_vs_fail_distinction():
    led = Ledger(":memory:")
    for i in range(100):  # clearly negative, but far below 300 games: still EXTEND (no early failure)
        add(led, i, clv=-0.05, league=["nba", "epl"][i % 2], venue=["polymarket", "kalshi"][(i // 2) % 2])
    g = graduation(led, cfg(), NOW)
    assert g["state"] == "extend" and "not a failure" in g["verdict"]
    fmt = format_graduation(g)
    assert "FAIL?" in fmt and "provisional" in fmt
    led2 = Ledger(":memory:")
    for i in range(300):
        add(led2, i, clv=-0.05, league=["nba", "epl"][i % 2], venue=["polymarket", "kalshi"][(i // 2) % 2])
    assert graduation(led2, cfg(), NOW)["state"] == "fail"


def test_sensitivity_reports_power_and_multiplicative_closes():
    led = Ledger(":memory:")
    e1 = add(led, 1, clv=0.10)
    add(led, 2, clv=0.10)  # no alternative close rows for this game
    led.capture_closing("g1", "a", "pinnacle_power", 0.60, T0)
    led.capture_closing("g1", "a", "pinnacle_multiplicative", 0.55, T0)
    sample = measured_sample(led, cfg())
    sens = status(led, cfg(), NOW)["sensitivity"]
    assert sens["power"]["games"] == 1 and sens["power"]["mean_clv"] == pytest.approx(0.60 / 0.50 - 1)
    assert sens["multiplicative"]["mean_clv"] == pytest.approx(0.55 / 0.50 - 1)
    assert len(sample) == 2 and e1
    txt = format_status(status(led, cfg(), NOW))
    assert "power:" in txt and "multiplicative:" in txt


def test_both_devig_closes_are_captured_by_the_engine(http):
    from tests.test_quota_scheduler import LMX_KICK, make, mins, settings as qsettings

    s = qsettings(max_sharp_age_minutes=60, leagues=["ligamx"], min_edge=0.05)
    eng = make(http, s)
    eng.cycle(LMX_KICK - mins(50))
    gk = eng.ledger.entries()[0]["game_key"]
    p = eng.ledger.closing(gk, "necaxa", "pinnacle_power")
    m = eng.ledger.closing(gk, "necaxa", "pinnacle_multiplicative")
    assert p is not None and m is not None and p != m  # longshot gets less margin under power
    assert p < m
    both = [eng.ledger.closing(gk, k, "pinnacle_power") for k in ("necaxa", "guadalajara", "draw")]
    assert sum(both) == pytest.approx(1.0, abs=1e-6)


def test_no_close_entries_are_marked_and_excluded_from_the_sample():
    led = Ledger(":memory:")
    add(led, 1)
    add(led, 2, status_="no_close", close=False)
    st = status(led, cfg(), NOW)
    assert st["no_close"] == 1 and st["games_measured"] == 1 and "NO CLOSE" in format_status(st)


def test_retroactive_rate_under_new_filters_and_deadline_warning():
    led = Ledger(":memory:")
    for i in range(20):  # 20 old entries over 20 days: half pass the new filters
        add(led, i, day=NOW - timedelta(days=40) + timedelta(days=i), edge=0.08 if i % 2 == 0 else 0.03, ref="pinnacle" if i < 16 else "consensus")
    rt = status(led, cfg(), NOW)["retro"]
    assert rt["entries"] == 8 and rt["entries_per_day"] == pytest.approx(8 / 40)
    assert "upper bound" in rt["note"]
    st = status(led, cfg(), NOW)
    assert st["deadline_warning"].startswith("WARNING") and "2027-01-27" in st["deadline_warning"]
    assert "WARNING" in format_status(st)


def test_deadline_warning_cannot_confirm_without_rate_and_clears_when_on_pace():
    st = status(Ledger(":memory:"), cfg(), NOW)
    assert "cannot confirm" in st["deadline_warning"] and "would need" in st["deadline_warning"]
    led = Ledger(":memory:")
    for i in range(120):  # ~6 games/day for 20 days => 300 games within the ~68 days left
        add(led, i, day=NOW - timedelta(days=20) + timedelta(hours=4 * i))
    st = status(led, cfg(), NOW)
    assert st["games_per_day"] == pytest.approx(6.0) and st["deadline_warning"] == ""
    slow = Ledger(":memory:")
    for i in range(30):
        add(slow, i, day=NOW - timedelta(days=20) + timedelta(hours=16 * i))
    assert "pace too slow" in status(slow, cfg(), NOW)["deadline_warning"]
    assert "passed" in status(slow, cfg(grad_deadline="2026-11-01"), NOW)["deadline_warning"]


def test_preregistered_rules_in_output_and_doc():
    g = graduation(Ledger(":memory:"), cfg(), NOW)
    assert "TAKER" in g["rules"]["fills"] and "scheduled kickoff" in g["rules"]["close"] and "multiplicative" in g["rules"]["clv"]
    assert "Rules fixed before measuring" in format_graduation(g)
