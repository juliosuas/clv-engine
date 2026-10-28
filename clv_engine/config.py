"""Settings from environment variables (optionally a local .env file).

Secrets (ODDS_API_KEY, webhook URLs, Telegram token) are only ever read from the
environment; never commit them. See .env.example for every knob.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Settings of the old per-day, tier-based Odds API scheduler. They are ignored now (a warning names the ones still set).
REMOVED_ENV = ("ODDS_API_DAILY_BUDGET", "SHARP_POLL_SECONDS", "SHARP_CLOSE_POLL_SECONDS", "SHARP_NEAR_POLL_SECONDS", "SHARP_IDLE_POLL_SECONDS",
               "CLOSE_CAPTURE_MINUTES", "NEAR_WINDOW_MINUTES", "IDLE_AFTER_MINUTES", "CLOSE_DENSE_PENDING_ONLY", "PLAN_KICKOFF_CLUSTERS")


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides real env vars."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    return float(raw) if raw.strip() else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.strip() else default


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    db_path: str = "./data/clv.sqlite"
    # Order = priority: leagues are processed (and Odds API credits spent) in this order.
    leagues: list[str] = field(default_factory=lambda: ["nba", "nfl", "epl", "laliga", "ligamx", "ucl"])
    # Temporary scope cut: no NEW entries and no Pinnacle polling for these leagues / venues
    # (entries already in the ledger still get their close and settlement).
    excluded_leagues: list[str] = field(default_factory=lambda: ["ucl"])
    excluded_venues: list[str] = field(default_factory=lambda: ["kalshi"])
    poll_seconds: int = 120
    lookahead_hours: float = 72
    match_window_hours: float = 30

    sharp_source: str = "none"
    odds_api_key: str = field(default="", repr=False)
    sharp_book: str = "pinnacle"
    soft_books: list[str] = field(default_factory=lambda: ["codere_it", "betsson", "williamhill", "unibet_eu"])
    devig_method: str = "power"
    odds_api_historical: bool = False
    odds_api_regions: str = "eu"
    # --- Odds API credit scheduler (see clv_engine/sharp/quota.py): free plan = 500 credits per CALENDAR MONTH ---
    # Plan size and the local hard cap (never spend past it; the provider's x-requests-* headers are the truth for used/remaining).
    odds_api_monthly_budget: int = 500
    odds_api_hard_cap: int = 450
    # The daily allowance is the remaining spendable credits / calendar days left, capped at this multiple of the even share of the hard cap.
    odds_api_daily_burst: float = 3.0
    # Close window per kickoff round, in minutes before the round's first kickoff: polls happen between T-MAX and T-MIN.
    close_window_max_minutes: int = 5
    close_window_min_minutes: int = 1
    # 1 = a single (final) close poll per round; 2 = an early one at the start of the window plus the final one.
    close_polls_per_round: int = 1
    # One entry poll per round this many minutes before its first kickoff (must be >= MAX_SHARP_AGE_MINUTES + CLOSE_WINDOW_MAX_MINUTES to cover entries).
    entry_poll_lead_minutes: int = 25
    # Share of the daily allowance reserved for close polls (entry polls may use only the rest).
    odds_api_close_reserve: float = 0.50
    # Hard floor on the provider-reported x-requests-remaining: below it only close polls run.
    odds_api_min_remaining: int = 100
    # Assumed credits per /odds call; 0 = derive (1 market x ceil(books/10) regions); replaced by x-requests-last when sent.
    odds_api_credits_per_call: int = 0
    # Pinnacle data older than this is not used for entries or closing capture (budget exhausted, outage).
    max_sharp_age_minutes: float = 20
    # A Pinnacle close only counts if sampled this close to kickoff (0 disables the check).
    close_max_age_minutes: float = 20
    # Planning-only assumption for the credit estimate: kickoff rounds per league per day.
    plan_rounds_per_league_day: float = 2.0

    entry_reference: str = "auto"  # auto | sharp | consensus
    # Entry gate (proposal, Julio to approve): edge vs Pinnacle no-vig fair, AFTER fees, >= 5%.
    min_edge: float = 0.05
    # Only Pinnacle may justify an entry (consensus fallback no longer opens paper entries).
    require_pinnacle_ref: bool = True
    # Minimum ask depth (USD notional) within depth_tolerance of the best ask; the full STAKE_USD
    # must also fill (ALLOW_PARTIAL=0). 0 disables the depth check.
    allow_hedged_entries: bool = False
    min_depth_usd: float = 250.0
    depth_tolerance: float = 0.01
    stake_usd: float = 100.0
    allow_partial: bool = False
    max_entries_per_outcome: int = 1
    min_minutes_before_start: float = 5
    entry_sides: list[str] = field(default_factory=lambda: ["yes", "no"])
    min_price: float = 0.05  # ignore deep longshots / near-certainties (noise, tiny payout)
    max_price: float = 0.95
    # Soft-book paper entries need soft books in the /odds request; the default Pinnacle-only poll (1 credit) cannot feed them.
    soft_entries: bool = False
    soft_edge: float = 0.03

    kalshi_fee_rounding: str = "cent"
    fetch_no_books: bool = True

    arb_min_profit_usd: float = 1.0
    arb_min_roi: float = 0.005
    arb_max_shares: float = 2000

    alert_sinks: list[str] = field(default_factory=lambda: ["stdout", "file"])
    alert_file: str = "./data/alerts.jsonl"
    alert_webhook_url: str = field(default="", repr=False)
    alert_webhook_kind: str = "slack"  # slack | discord | telegram | generic
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    alert_cooldown_seconds: int = 1800

    snapshot_min_seconds: int = 600
    settle_after_hours: float = 3.0
    report_dir: str = "./reports"
    report_tz: str = "America/Mexico_City"
    weekly_report_weekday: int = 0  # 0=Monday ... 6=Sunday, -1 disables
    weekly_report_hour: int = 9
    log_level: str = "INFO"

    # --- graduation gate (proposed thresholds, not approved facts; see docs/GRADUATION.md) ---
    grad_min_games: int = 300
    grad_ci_level: float = 0.90
    grad_min_group_games: int = 30
    grad_min_leagues: int = 2
    grad_min_venues: int = 2
    grad_max_league_share: float = 0.70
    grad_min_settled_fraction: float = 0.90
    grad_bootstrap_samples: int = 5000
    grad_deadline: str = "2027-01-27"

    @classmethod
    def from_env(cls, dotenv: str | None = ".env") -> "Settings":
        if dotenv:
            load_dotenv(dotenv)
        d = cls()
        gone = sorted(k for k in REMOVED_ENV if os.environ.get(k))
        if gone:
            log.warning("Ignoring removed setting(s) %s: the Odds API scheduler is now monthly and round-based "
                        "(ODDS_API_MONTHLY_BUDGET / ODDS_API_HARD_CAP / CLOSE_WINDOW_* / ENTRY_POLL_LEAD_MINUTES; see README \"Credit budget\")", ", ".join(gone))
        return cls(
            db_path=os.environ.get("DB_PATH", d.db_path),
            leagues=_list("LEAGUES", ",".join(d.leagues)),
            excluded_leagues=[x.lower() for x in _list("EXCLUDED_LEAGUES", ",".join(d.excluded_leagues))],
            excluded_venues=[x.lower() for x in _list("EXCLUDED_VENUES", ",".join(d.excluded_venues))],
            poll_seconds=_i("POLL_SECONDS", d.poll_seconds),
            lookahead_hours=_f("LOOKAHEAD_HOURS", d.lookahead_hours),
            match_window_hours=_f("MATCH_WINDOW_HOURS", d.match_window_hours),
            sharp_source=os.environ.get("SHARP_SOURCE", d.sharp_source).strip().lower() or "none",
            odds_api_key=os.environ.get("ODDS_API_KEY", "").strip(),
            sharp_book=os.environ.get("SHARP_BOOK", d.sharp_book),
            soft_books=_list("SOFT_BOOKS", ",".join(d.soft_books)),
            devig_method=os.environ.get("DEVIG_METHOD", d.devig_method),
            odds_api_historical=_b("ODDS_API_HISTORICAL", d.odds_api_historical),
            odds_api_regions=os.environ.get("ODDS_API_REGIONS", d.odds_api_regions).strip().lower() or d.odds_api_regions,
            odds_api_monthly_budget=_i("ODDS_API_MONTHLY_BUDGET", d.odds_api_monthly_budget),
            odds_api_hard_cap=_i("ODDS_API_HARD_CAP", d.odds_api_hard_cap),
            odds_api_daily_burst=_f("ODDS_API_DAILY_BURST", d.odds_api_daily_burst),
            close_window_max_minutes=_i("CLOSE_WINDOW_MAX_MINUTES", d.close_window_max_minutes),
            close_window_min_minutes=_i("CLOSE_WINDOW_MIN_MINUTES", d.close_window_min_minutes),
            close_polls_per_round=_i("CLOSE_POLLS_PER_ROUND", d.close_polls_per_round),
            entry_poll_lead_minutes=_i("ENTRY_POLL_LEAD_MINUTES", d.entry_poll_lead_minutes),
            odds_api_close_reserve=_f("ODDS_API_CLOSE_RESERVE", d.odds_api_close_reserve),
            odds_api_min_remaining=_i("ODDS_API_MIN_REMAINING", d.odds_api_min_remaining),
            odds_api_credits_per_call=_i("ODDS_API_CREDITS_PER_CALL", d.odds_api_credits_per_call),
            max_sharp_age_minutes=_f("MAX_SHARP_AGE_MINUTES", d.max_sharp_age_minutes),
            close_max_age_minutes=_f("CLOSE_MAX_AGE_MINUTES", d.close_max_age_minutes),
            plan_rounds_per_league_day=_f("PLAN_ROUNDS_PER_LEAGUE_DAY", d.plan_rounds_per_league_day),
            entry_reference=os.environ.get("ENTRY_REFERENCE", d.entry_reference).strip().lower(),
            min_edge=_f("MIN_EDGE", d.min_edge),
            require_pinnacle_ref=_b("REQUIRE_PINNACLE_REF", d.require_pinnacle_ref),
            allow_hedged_entries=_b("ALLOW_HEDGED_ENTRIES", d.allow_hedged_entries),
            min_depth_usd=_f("MIN_DEPTH_USD", d.min_depth_usd),
            depth_tolerance=_f("DEPTH_TOLERANCE", d.depth_tolerance),
            stake_usd=_f("STAKE_USD", d.stake_usd),
            allow_partial=_b("ALLOW_PARTIAL", d.allow_partial),
            max_entries_per_outcome=_i("MAX_ENTRIES_PER_OUTCOME", d.max_entries_per_outcome),
            min_minutes_before_start=_f("MIN_MINUTES_BEFORE_START", d.min_minutes_before_start),
            entry_sides=_list("ENTRY_SIDES", ",".join(d.entry_sides)),
            min_price=_f("MIN_PRICE", d.min_price),
            max_price=_f("MAX_PRICE", d.max_price),
            soft_entries=_b("SOFT_ENTRIES", d.soft_entries),
            soft_edge=_f("SOFT_EDGE", d.soft_edge),
            kalshi_fee_rounding=os.environ.get("KALSHI_FEE_ROUNDING", d.kalshi_fee_rounding),
            fetch_no_books=_b("FETCH_NO_BOOKS", d.fetch_no_books),
            arb_min_profit_usd=_f("ARB_MIN_PROFIT_USD", d.arb_min_profit_usd),
            arb_min_roi=_f("ARB_MIN_ROI", d.arb_min_roi),
            arb_max_shares=_f("ARB_MAX_SHARES", d.arb_max_shares),
            alert_sinks=_list("ALERT_SINKS", ",".join(d.alert_sinks)),
            alert_file=os.environ.get("ALERT_FILE", d.alert_file),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL", "").strip(),
            alert_webhook_kind=os.environ.get("ALERT_WEBHOOK_KIND", d.alert_webhook_kind).strip().lower(),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            alert_cooldown_seconds=_i("ALERT_COOLDOWN_SECONDS", d.alert_cooldown_seconds),
            snapshot_min_seconds=_i("SNAPSHOT_MIN_SECONDS", d.snapshot_min_seconds),
            settle_after_hours=_f("SETTLE_AFTER_HOURS", d.settle_after_hours),
            report_dir=os.environ.get("REPORT_DIR", d.report_dir),
            report_tz=os.environ.get("REPORT_TZ", d.report_tz),
            weekly_report_weekday=_i("WEEKLY_REPORT_WEEKDAY", d.weekly_report_weekday),
            weekly_report_hour=_i("WEEKLY_REPORT_HOUR", d.weekly_report_hour),
            log_level=os.environ.get("LOG_LEVEL", d.log_level),
            grad_min_games=_i("GRAD_MIN_GAMES", d.grad_min_games),
            grad_ci_level=_f("GRAD_CI_LEVEL", d.grad_ci_level),
            grad_min_group_games=_i("GRAD_MIN_GROUP_GAMES", d.grad_min_group_games),
            grad_min_leagues=_i("GRAD_MIN_LEAGUES", d.grad_min_leagues),
            grad_min_venues=_i("GRAD_MIN_VENUES", d.grad_min_venues),
            grad_max_league_share=_f("GRAD_MAX_LEAGUE_SHARE", d.grad_max_league_share),
            grad_min_settled_fraction=_f("GRAD_MIN_SETTLED_FRACTION", d.grad_min_settled_fraction),
            grad_bootstrap_samples=_i("GRAD_BOOTSTRAP_SAMPLES", d.grad_bootstrap_samples),
            grad_deadline=os.environ.get("GRAD_DEADLINE", d.grad_deadline),
        )
