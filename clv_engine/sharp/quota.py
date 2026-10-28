"""Odds API credit scheduling: cadence tiers, a daily budget and a planning estimate.

What is documented by the provider (https://the-odds-api.com/liveapi/guides/v4/, checked 2026-10-27):
  * an /odds call costs  markets x regions  credits; every 10 bookmakers count as one region;
  * the response headers x-requests-remaining / x-requests-used / x-requests-last report the account state;
  * calls that return no events are not charged.

What is NOT verified here (no API key available when this was written): the credits included in
your plan and the period after which x-requests-remaining resets. Nothing below hardcodes a plan.
ODDS_API_DAILY_BUDGET is a local planning number you set; the provider-reported remaining
credits (ODDS_API_MIN_REMAINING) act as an additional hard floor.

Cadence tiers (seconds between /odds calls per league), by time to the nearest relevant kickoff:
  close  <= CLOSE_CAPTURE_MINUTES to a kickoff with pending entries -> SHARP_CLOSE_POLL_SECONDS
  near   <= NEAR_WINDOW_MINUTES                                      -> SHARP_NEAR_POLL_SECONDS
  far    <= IDLE_AFTER_MINUTES                                       -> SHARP_POLL_SECONDS
  idle   nothing known / further out                                 -> SHARP_IDLE_POLL_SECONDS
The engine loop itself runs every POLL_SECONDS, so the effective cadence is max(tier, POLL_SECONDS).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

CLOSE, NEAR, FAR, IDLE = "close", "near", "far", "idle"
HISTORICAL_COST_MULTIPLIER = 10  # documented: historical endpoints cost 10x


def credits_per_call(n_books: int, n_markets: int = 1) -> int:
    """markets x regions, one region per 10 bookmakers (documented formula)."""
    return max(1, n_markets) * max(1, math.ceil(max(1, n_books) / 10))


def plan_league(now: datetime, known_kickoffs: list[datetime], pending_kickoffs: list[datetime], s: Any) -> tuple[str, int, float | None]:
    """Pick (tier, interval seconds, minutes to nearest upcoming kickoff) for one league."""
    upcoming_known = [k for k in known_kickoffs if k >= now]
    upcoming_pending = [k for k in pending_kickoffs if k >= now]

    def mins(ks: list[datetime]) -> float:
        return min((k - now).total_seconds() / 60 for k in ks)

    dense_pool = upcoming_pending if s.close_dense_pending_only else upcoming_known + upcoming_pending
    if dense_pool and mins(dense_pool) <= s.close_capture_minutes:
        return CLOSE, s.sharp_close_poll_seconds, mins(dense_pool)
    allk = upcoming_known + upcoming_pending
    if not allk:
        return IDLE, s.sharp_idle_poll_seconds, None
    m = mins(allk)
    if m <= s.near_window_minutes:
        return NEAR, s.sharp_near_poll_seconds, m
    if m <= s.idle_after_minutes:
        return FAR, s.sharp_poll_seconds, m
    return IDLE, s.sharp_idle_poll_seconds, m


class QuotaTracker:
    """Per-UTC-day credit accounting, persisted in the ledger (api_usage) when one is given."""

    def __init__(self, settings: Any, ledger: Any = None, n_books: int = 1):
        self.s = settings
        self.ledger = ledger
        self.est_cost = settings.odds_api_credits_per_call or credits_per_call(n_books)
        self._mem: dict[str, dict[str, Any]] = {}
        self.last_remaining: int | None = None
        self.last_used: int | None = None

    @staticmethod
    def day(now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    def usage(self, now: datetime) -> dict[str, Any]:
        d = self.day(now)
        if d not in self._mem:
            self._mem[d] = self.ledger.usage(d) if self.ledger else {"day": d, "requests": 0, "credits": 0.0, "skipped": 0, "remaining": None, "used": None}
            if self.last_remaining is None and self._mem[d].get("remaining") is not None:
                self.last_remaining = self._mem[d]["remaining"]
        return self._mem[d]

    def spent_today(self, now: datetime) -> float:
        return float(self.usage(now)["credits"])

    def decide(self, now: datetime, essential: bool, multiplier: int = 1) -> tuple[bool, str]:
        """May we spend credits now? essential = close-window polls for pending entries."""
        cost = self.est_cost * multiplier
        budget = float(self.s.odds_api_daily_budget)
        spent = self.spent_today(now)
        rem = self.last_remaining
        if rem is not None and rem <= 0:
            return False, "provider reports x-requests-remaining=0"
        if rem is not None and rem < cost:
            return False, f"provider x-requests-remaining={rem} < call cost {cost}"
        if essential:
            if spent + cost > budget:
                return False, f"daily budget exhausted ({spent:.0f}+{cost}>{budget:.0f} credits)"
            return True, "ok"
        normal_cap = budget * (1.0 - self.s.odds_api_close_reserve)
        if spent + cost > normal_cap:
            return False, f"non-close budget used ({spent:.0f}+{cost}>{normal_cap:.0f} of {budget:.0f}; rest reserved for close polls)"
        if rem is not None and rem <= self.s.odds_api_min_remaining:
            return False, f"provider x-requests-remaining={rem} <= ODDS_API_MIN_REMAINING={self.s.odds_api_min_remaining}; close polls only"
        return True, "ok"

    def record_call(self, now: datetime, headers: dict[str, str] | None, multiplier: int = 1) -> float:
        h = headers or {}
        spent = _num(h.get("x-requests-last"))
        credits = float(spent) if spent is not None else float(self.est_cost * multiplier)
        rem, used = _num(h.get("x-requests-remaining")), _num(h.get("x-requests-used"))
        if rem is not None:
            self.last_remaining = int(rem)
        if used is not None:
            self.last_used = int(used)
        u = self.usage(now)
        u["requests"] += 1
        u["credits"] += credits
        if rem is not None:
            u["remaining"] = int(rem)
        if used is not None:
            u["used"] = int(used)
        if self.ledger:
            self.ledger.usage_add(self.day(now), now, credits=credits, requests=1,
                                  remaining=None if rem is None else int(rem), used=None if used is None else int(used))
        return credits

    def record_skip(self, now: datetime) -> None:
        self.usage(now)["skipped"] += 1
        if self.ledger:
            self.ledger.usage_add(self.day(now), now, skipped=1)


def _num(v: Any) -> float | None:
    try:
        return None if v is None or str(v).strip() == "" else float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class PlanEstimate:
    leagues: int
    calls_per_league_day: float
    credits_per_day: float
    credits_per_30_days: float
    budget: int
    over_budget: bool
    breakdown: dict[str, float]


def estimate_plan(s: Any, n_leagues: int, n_books: int | None = None) -> PlanEstimate:
    """Planning estimate of /odds calls and credits per day (an assumption model, not a measurement).

    Assumes PLAN_KICKOFF_CLUSTERS distinct kickoff slots per league per day, each with a pending entry
    (so the dense close tier applies), and that the tiers do not overlap beyond one day.
    """
    n_books = n_books if n_books is not None else 1 + len(s.soft_books)
    cost = s.odds_api_credits_per_call or credits_per_call(n_books)
    day_min = 24 * 60
    clusters = max(0, s.plan_kickoff_clusters)
    span = clusters * s.idle_after_minutes
    scale = min(1.0, day_min / span) if span else 1.0
    w, nw, ia = s.close_capture_minutes, s.near_window_minutes, s.idle_after_minutes
    mins = {
        CLOSE: clusters * w * scale,
        NEAR: clusters * max(0, nw - w) * scale,
        FAR: clusters * max(0, ia - nw) * scale,
    }
    mins[IDLE] = max(0.0, day_min - sum(mins.values()))
    interval = {CLOSE: s.sharp_close_poll_seconds, NEAR: s.sharp_near_poll_seconds, FAR: s.sharp_poll_seconds, IDLE: s.sharp_idle_poll_seconds}
    breakdown = {t: m * 60 / max(interval[t], s.poll_seconds, 1) for t, m in mins.items()}
    calls = sum(breakdown.values())
    credits = calls * cost * max(0, n_leagues)
    return PlanEstimate(n_leagues, calls, credits, credits * 30, s.odds_api_daily_budget, credits > s.odds_api_daily_budget, breakdown)


def format_plan(s: Any, n_leagues: int, n_books: int | None = None) -> str:
    p = estimate_plan(s, n_leagues, n_books)
    parts = ", ".join(f"{t}={c:.0f}" for t, c in p.breakdown.items())
    verdict = ("OVER budget: far/near polls will be throttled first (close polls keep a reserved share)" if p.over_budget else "within budget")
    return (f"Odds API credit plan (estimate, assumes {s.plan_kickoff_clusters} kickoff slots/league/day; plan limits UNVERIFIED): "
            f"{n_leagues} leagues x {p.calls_per_league_day:.0f} calls/league/day ({parts}) = ~{p.credits_per_day:.0f} credits/day "
            f"(~{p.credits_per_30_days:.0f}/30d) vs ODDS_API_DAILY_BUDGET={p.budget}: {verdict}")

