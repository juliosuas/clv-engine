from __future__ import annotations

from datetime import datetime

from ..leagues import League
from ..models import SharpGame


class SharpSource:
    """Interface: return de-vigged Pinnacle lines (+ optional soft books) per league."""

    name = "base"
    available = False

    def fetch(self, league: League, now: datetime | None = None) -> list[SharpGame]:  # pragma: no cover - interface
        raise NotImplementedError

    def closing(self, league: League, game_ref: str, start: datetime, now: datetime | None = None) -> SharpGame | None:
        """Optional backfill of the closing line (e.g. a historical endpoint)."""
        return None

    # --- scheduling hooks (no-ops for sources without a credit budget) ---
    closing_blocked = False

    def set_pending(self, league_key: str, kickoffs: list[datetime]) -> None:
        """Kickoffs of games that still have entries waiting for a Pinnacle close."""

    def fetched_at(self, league_key: str) -> datetime | None:
        """When the data last returned by fetch() was actually sampled from the provider."""
        return None


class NoSharpSource(SharpSource):
    """Used when no paid feed is configured: everything else runs, CLV stays pending."""

    name = "none"
    available = False

    def __init__(self, reason: str = "no sharp source configured"):
        self.reason = reason

    def fetch(self, league: League, now: datetime | None = None) -> list[SharpGame]:
        return []
