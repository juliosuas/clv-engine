"""Small parsing helpers.

safe_float / parse_json_field / parse_datetime are adapted from
juliosuas/polymarket-desk (api/common.py, MIT).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(default if value is None else value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def parse_json_field(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def parse_datetime(value: Any) -> datetime | None:
    """Parse ISO-ish timestamps ("2026-10-20T19:00:00Z", "2026-10-20 19:00:00+00")."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # "+00" -> "+00:00"
    if len(text) >= 3 and text[-3] in "+-" and text[-2:].isdigit():
        text = text + ":00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
