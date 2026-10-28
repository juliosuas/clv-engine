"""Odds API credit scheduling for a small MONTHLY plan (free tier: 500 credits per month).

What is documented by the provider (https://the-odds-api.com/liveapi/guides/v4/, re-checked 2026-10-27):
  * a /odds call costs  markets x regions  credits; `bookmakers` takes priority over `regions`, and every 10 bookmakers count as one
    region, so `bookmakers=pinnacle&markets=h2h` is 1 credit per call and returns every event of the sport;
  * calls that return no events are not charged; /sports and /events are free;
  * the response headers x-requests-remaining / x-requests-used / x-requests-last report the account state
    ("until the quota resets" / "since the last quota reset").
Not verified here: the exact reset instant of the quota. The engine treats the UTC calendar month as the period; the provider
headers are the truth inside the month, and headers last seen in an earlier month are ignored (the counters may have reset).

Money-saving shape (see README "Credit budget"):
  * kickoffs are grouped into ROUNDS per sport (kickoffs within CLOSE_WINDOW_MAX-CLOSE_WINDOW_MIN minutes of the round's first one),
    so ONE /odds poll per sport serves every game of the round;
  * per round at most: one ENTRY poll at T-ENTRY_POLL_LEAD (fresh Pinnacle lines for entries until T-CLOSE_WINDOW_MAX) and
    CLOSE_POLLS_PER_ROUND close polls inside T-5..T-1 minutes (the last one is the "final" close); never historical;
  * credit priority: final close polls (essential: only the hard cap and the provider's remaining stop them) > early close polls >
    entry polls. Optional polls also respect a daily pace derived from what is left of the month and must leave enough credits for the
    final closes of the rounds that already have pending entries.
"""

from __future__ import annotations

import calendar
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# kinds of /odds polls, highest priority first
FINAL, EARLY, ENTRY, HISTORICAL = "close_final", "close_early", "entry", "historical"
HISTORICAL_COST_MULTIPLIER = 10  # documented: historical endpoints cost 10x


def credits_per_call(n_books: int, n_markets: int = 1) -> int:
    """markets x regions, one region per 10 bookmakers (documented formula)."""
    return max(1, n_markets) * max(1, math.ceil(max(1, n_books) / 10))


# ---- rounds ------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Round:
    start: datetime          # first kickoff of the round
    end: datetime            # last kickoff grouped into it
    pending: bool            # at least one entry of this round still waits for its Pinnacle close

    def contains(self, t: datetime, slack_minutes: float = 0.0) -> bool:
        return self.start - timedelta(minutes=slack_minutes) <= t <= self.end + timedelta(minutes=slack_minutes)


def round_width_minutes(s: Any) -> float:
    return max(0.0, float(s.close_window_max_minutes - s.close_window_min_minutes))


def build_rounds(now: datetime, kickoffs: list[datetime], pending: list[datetime], s: Any) -> list[Round]:
    """Group upcoming kickoffs (known + pending, no duplicates) into rounds; a round is `pending` if a pending kickoff falls in it."""
    width = timedelta(minutes=round_width_minutes(s))
    pend = sorted({k for k in pending if k >= now})
    allk = sorted({k for k in kickoffs if k >= now} | set(pend))
    rounds: list[Round] = []
    cur: list[datetime] = []
    for k in allk:
        if cur and k - cur[0] > width:
            rounds.append(Round(cur[0], cur[-1], any(cur[0] <= p <= cur[-1] for p in pend)))
            cur = []
        cur.append(k)
    if cur:
        rounds.append(Round(cur[0], cur[-1], any(cur[0] <= p <= cur[-1] for p in pend)))
    return rounds


@dataclass(frozen=True)
class PollDue:
    kind: str
    round: Round


def next_poll(now: datetime, rounds: list[Round], fetched_at: datetime | None, polls: list[datetime], s: Any) -> PollDue | None:
    """What (if anything) must be polled for this sport right now. Pure function of the clock and the sport's poll history.

    `polls` = times of the sport's successful polls. The engine loop runs every POLL_SECONDS, so "the last cycle of the close window"
    is the cycle after which the next one would fall past T-CLOSE_WINDOW_MIN."""
    cyc = timedelta(seconds=max(1, s.poll_seconds))
    for r in rounds:
        t = r.start
        win_start = t - timedelta(minutes=s.close_window_max_minutes)
        win_final = t - timedelta(minutes=s.close_window_min_minutes)
        if r.pending and win_start <= now < t:
            have = [p for p in polls if win_start <= p <= t]
            last_cycle = now + cyc > win_final
            if not have:
                if last_cycle:
                    return PollDue(FINAL, r)
                if s.close_polls_per_round >= 2:
                    return PollDue(EARLY, r)
            elif len(have) < min(2, s.close_polls_per_round) and last_cycle and now - max(have) >= cyc * 0.5:
                return PollDue(FINAL, r)
        entry_start = t - timedelta(minutes=s.entry_poll_lead_minutes)
        if entry_start <= now < win_start and (fetched_at is None or fetched_at < entry_start):
            return PollDue(ENTRY, r)
    return None


# ---- budget ------------------------------------------------------------------------------------------------------------------
class QuotaTracker:
    """Credit accounting per UTC day AND per UTC calendar month, persisted in the ledger (api_usage) when one is given.

    The provider headers are the source of truth for used/remaining; local counts fill in until the first header of a month."""

    def __init__(self, settings: Any, ledger: Any = None, n_books: int = 1):
        self.s = settings
        self.ledger = ledger
        self.est_cost = settings.odds_api_credits_per_call or credits_per_call(n_books)
        self._mem: dict[str, dict[str, Any]] = {}
        self._month_mem: dict[str, dict[str, Any]] = {}
        self.last_remaining: int | None = None
        self.last_used: int | None = None
        self._hdr_month: str | None = None  # month in which last_remaining/last_used were reported

    @staticmethod
    def day(now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    @staticmethod
    def month(now: datetime) -> str:
        return now.strftime("%Y-%m")

    def usage(self, now: datetime) -> dict[str, Any]:
        d = self.day(now)
        if d not in self._mem:
            self._mem[d] = self.ledger.usage(d) if self.ledger else {"day": d, "requests": 0, "credits": 0.0, "skipped": 0, "remaining": None, "used": None}
        return self._mem[d]

    def month_usage(self, now: datetime) -> dict[str, Any]:
        m = self.month(now)
        if m not in self._month_mem:
            self._month_mem[m] = self.ledger.month_usage(m) if self.ledger else {"month": m, "requests": 0, "credits": 0.0, "skipped": 0, "remaining": None, "used": None}
            if self.last_remaining is None and self._month_mem[m].get("remaining") is not None:
                self.last_remaining, self.last_used, self._hdr_month = self._month_mem[m]["remaining"], self._month_mem[m]["used"], m
        return self._month_mem[m]

    def spent_today(self, now: datetime) -> float:
        return float(self.usage(now)["credits"])

    # -- the month ---------------------------------------------------------------------------------------------------------
    def used_this_month(self, now: datetime) -> float:
        """x-requests-used when reported this month (includes credits spent by anything else on the key), else the local count."""
        self.month_usage(now)
        if self.last_used is not None and self._hdr_month == self.month(now):
            return float(self.last_used)
        return float(self.month_usage(now)["credits"])

    def remaining_now(self, now: datetime) -> int | None:
        self.month_usage(now)
        return self.last_remaining if self._hdr_month == self.month(now) else None

    def spendable(self, now: datetime) -> float:
        """Credits that may still be spent this month: below the hard cap AND below what the provider says is left."""
        used = self.used_this_month(now)
        cap = float(self.s.odds_api_hard_cap) - used
        rem = self.remaining_now(now)
        left = (float(rem) if rem is not None else float(self.s.odds_api_monthly_budget) - used)
        return max(0.0, min(cap, left))

    def days_left(self, now: datetime) -> int:
        return calendar.monthrange(now.year, now.month)[1] - now.day + 1

    def daily_allowance(self, now: datetime) -> float:
        """Even split of what is spendable over the remaining calendar days (today included), never more than
        ODDS_API_DAILY_BURST x the even share of the hard cap (the reset instant is unverified: do not burn the month in a few days)."""
        dim = calendar.monthrange(now.year, now.month)[1]
        pace = (self.spendable(now) + self.spent_today(now)) / max(1, self.days_left(now))
        return min(pace, self.s.odds_api_daily_burst * float(self.s.odds_api_hard_cap) / dim)

    def decide(self, now: datetime, kind: str, multiplier: int = 1, committed: float = 0.0) -> tuple[bool, str]:
        """May we spend credits now? `committed` = credits to keep for the final closes of rounds that already have pending entries."""
        cost = self.est_cost * multiplier
        used, rem, spend = self.used_this_month(now), self.remaining_now(now), self.spendable(now)
        if rem is not None and rem <= 0:
            return False, "provider reports x-requests-remaining=0"
        if rem is not None and rem < cost:
            return False, f"provider x-requests-remaining={rem} < call cost {cost}"
        if spend < cost:
            return False, (f"monthly hard cap ODDS_API_HARD_CAP={self.s.odds_api_hard_cap} reached ({used:.0f} used this month, "
                           f"budget {self.s.odds_api_monthly_budget}, call cost {cost})")
        if kind == FINAL:
            return True, "ok"
        if spend - cost < committed:
            return False, f"{spend:.0f} spendable credits are kept for {committed:.0f} credits of final close polls already owed"
        allowance = self.daily_allowance(now)
        spent = self.spent_today(now)
        if kind == EARLY:
            if spent + cost > allowance:
                return False, f"daily pace used ({spent:.0f}+{cost}>{allowance:.1f} credits/day for {self.days_left(now)} day(s) left)"
            return True, "ok"
        normal_cap = allowance * (1.0 - self.s.odds_api_close_reserve)
        if spent + cost > normal_cap:
            return False, f"non-close daily pace used ({spent:.0f}+{cost}>{normal_cap:.1f} of {allowance:.1f}; rest reserved for close polls)"
        if rem is not None and rem <= self.s.odds_api_min_remaining:
            return False, f"provider x-requests-remaining={rem} <= ODDS_API_MIN_REMAINING={self.s.odds_api_min_remaining}; close polls only"
        return True, "ok"

    def record_call(self, now: datetime, headers: dict[str, str] | None, multiplier: int = 1) -> float:
        h = headers or {}
        spent = _num(h.get("x-requests-last"))
        credits = float(spent) if spent is not None else float(self.est_cost * multiplier)
        rem, used = _num(h.get("x-requests-remaining")), _num(h.get("x-requests-used"))
        m = self.month_usage(now)
        if rem is not None or used is not None:
            self._hdr_month = self.month(now)
        if rem is not None:
            self.last_remaining = int(rem)
            m["remaining"] = int(rem)
        if used is not None:
            self.last_used = int(used)
            m["used"] = int(used)
        u = self.usage(now)
        u["requests"] += 1
        u["credits"] += credits
        m["requests"] += 1
        m["credits"] += credits
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
        self.month_usage(now)["skipped"] += 1
        if self.ledger:
            self.ledger.usage_add(self.day(now), now, skipped=1)


def _num(v: Any) -> float | None:
    try:
        return None if v is None or str(v).strip() == "" else float(v)
    except (TypeError, ValueError):
        return None


# ---- planning estimate (assumption model, no network) -------------------------------------------------------------------------
@dataclass
class PlanEstimate:
    leagues: int
    credits_per_round: float
    credits_per_day: float
    credits_per_30_days: float
    budget: int
    hard_cap: int
    over_budget: bool
    rounds_per_day_affordable: float


def estimate_plan(s: Any, n_leagues: int, n_books: int | None = None) -> PlanEstimate:
    """Planning estimate: per kickoff round of one sport = 1 entry poll + CLOSE_POLLS_PER_ROUND close polls.

    Assumes PLAN_ROUNDS_PER_LEAGUE_DAY kickoff rounds per league per day (an assumption, not a measurement)."""
    n_books = n_books if n_books is not None else 1
    cost = s.odds_api_credits_per_call or credits_per_call(n_books)
    per_round = (1 + max(0, min(2, s.close_polls_per_round))) * cost
    per_day = per_round * max(0.0, s.plan_rounds_per_league_day) * max(0, n_leagues)
    affordable = (s.odds_api_hard_cap / 30.0) / per_round if per_round else 0.0
    return PlanEstimate(n_leagues, per_round, per_day, per_day * 30, s.odds_api_monthly_budget, s.odds_api_hard_cap,
                        per_day * 30 > s.odds_api_hard_cap, affordable)


def format_plan(s: Any, n_leagues: int, n_books: int | None = None) -> str:
    p = estimate_plan(s, n_leagues, n_books)
    verdict = ("OVER the hard cap: entry polls are throttled first, then early closes; final closes keep priority; lost closes are marked no_close"
               if p.over_budget else "within the hard cap")
    return (f"Odds API credit plan (estimate, assumes {s.plan_rounds_per_league_day:g} kickoff rounds/league/day): {n_leagues} leagues x "
            f"{s.plan_rounds_per_league_day:g} rounds x {p.credits_per_round:.0f} credits/round (1 entry poll + {s.close_polls_per_round} close poll(s) "
            f"at T-{s.close_window_max_minutes}..T-{s.close_window_min_minutes} min) = ~{p.credits_per_day:.0f} credits/day (~{p.credits_per_30_days:.0f}/30d) "
            f"vs ODDS_API_HARD_CAP={p.hard_cap} (ODDS_API_MONTHLY_BUDGET={p.budget}): {verdict}; "
            f"affordable ~{p.rounds_per_day_affordable:.1f} rounds/day over all leagues")
