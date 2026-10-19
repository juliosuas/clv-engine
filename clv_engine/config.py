"""Settings from environment variables (optionally a local .env file).

Secrets (ODDS_API_KEY, webhook URLs, Telegram token) are only ever read from the
environment; never commit them. See .env.example for every knob.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    leagues: list[str] = field(default_factory=lambda: ["ligamx", "epl", "laliga", "ucl", "nba"])
    poll_seconds: int = 120
    lookahead_hours: float = 72
    match_window_hours: float = 30

    sharp_source: str = "none"
    odds_api_key: str = field(default="", repr=False)
    sharp_book: str = "pinnacle"
    soft_books: list[str] = field(default_factory=lambda: ["codere_it", "betsson", "williamhill", "unibet_eu"])
    devig_method: str = "power"
    sharp_poll_seconds: int = 900
    sharp_close_poll_seconds: int = 120
    close_capture_minutes: int = 30
    odds_api_historical: bool = False

    entry_reference: str = "auto"  # auto | sharp | consensus
    min_edge: float = 0.02
    stake_usd: float = 100.0
    allow_partial: bool = False
    max_entries_per_outcome: int = 1
    min_minutes_before_start: float = 5
    entry_sides: list[str] = field(default_factory=lambda: ["yes", "no"])
    min_price: float = 0.05  # ignore deep longshots / near-certainties (noise, tiny payout)
    max_price: float = 0.95
    soft_entries: bool = True
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

    @classmethod
    def from_env(cls, dotenv: str | None = ".env") -> "Settings":
        if dotenv:
            load_dotenv(dotenv)
        d = cls()
        return cls(
            db_path=os.environ.get("DB_PATH", d.db_path),
            leagues=_list("LEAGUES", ",".join(d.leagues)),
            poll_seconds=_i("POLL_SECONDS", d.poll_seconds),
            lookahead_hours=_f("LOOKAHEAD_HOURS", d.lookahead_hours),
            match_window_hours=_f("MATCH_WINDOW_HOURS", d.match_window_hours),
            sharp_source=os.environ.get("SHARP_SOURCE", d.sharp_source).strip().lower() or "none",
            odds_api_key=os.environ.get("ODDS_API_KEY", "").strip(),
            sharp_book=os.environ.get("SHARP_BOOK", d.sharp_book),
            soft_books=_list("SOFT_BOOKS", ",".join(d.soft_books)),
            devig_method=os.environ.get("DEVIG_METHOD", d.devig_method),
            sharp_poll_seconds=_i("SHARP_POLL_SECONDS", d.sharp_poll_seconds),
            sharp_close_poll_seconds=_i("SHARP_CLOSE_POLL_SECONDS", d.sharp_close_poll_seconds),
            close_capture_minutes=_i("CLOSE_CAPTURE_MINUTES", d.close_capture_minutes),
            odds_api_historical=_b("ODDS_API_HISTORICAL", d.odds_api_historical),
            entry_reference=os.environ.get("ENTRY_REFERENCE", d.entry_reference).strip().lower(),
            min_edge=_f("MIN_EDGE", d.min_edge),
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
        )
