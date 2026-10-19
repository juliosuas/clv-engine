"""Fair-price references and the configurable paper entry rule."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import MatchedGame, OutcomeQuote, VenueGame
from .orderbook import Fill, fill_for_stake


def sharp_fair(game: MatchedGame) -> dict[str, float] | None:
    if game.sharp and game.sharp.sharp:
        return dict(game.sharp.sharp.fair)
    return None


def venue_fair(vg: VenueGame) -> dict[str, float] | None:
    """Normalized mids of one venue (multiplicative de-vig of mid prices)."""
    if not vg.exhaustive:
        return None
    mids = {k: o.mid for k, o in vg.outcomes.items()}
    if any(m is None or m <= 0 for m in mids.values()):
        return None
    s = sum(mids.values())
    return {k: m / s for k, m in mids.items()}


def consensus_fair(game: MatchedGame, exclude_venue: str | None = None) -> dict[str, float] | None:
    """Average of other venues' normalized mids. Used only when no sharp feed is configured."""
    fairs = [f for v, vg in game.venues.items() if v != exclude_venue for f in [venue_fair(vg)] if f]
    if not fairs:
        return None
    keys = set.intersection(*(set(f) for f in fairs))
    if not keys:
        return None
    return {k: sum(f[k] for f in fairs) / len(fairs) for k in keys}


def reference(game: MatchedGame, venue: str, mode: str) -> tuple[str, dict[str, float]] | None:
    sf = sharp_fair(game)
    if mode in ("auto", "sharp") and sf:
        return "pinnacle", sf
    if mode in ("auto", "consensus"):
        cf = consensus_fair(game, exclude_venue=venue)
        if cf:
            return "consensus", cf
    return None


@dataclass
class Candidate:
    venue: str
    outcome: OutcomeQuote
    side: str
    fill: Fill
    fair: float
    ref_source: str
    sharp_fair: float | None

    @property
    def edge(self) -> float:
        return self.fair / self.fill.effective_price - 1.0 if self.fill.shares else -1.0


def evaluate(game: MatchedGame, vg: VenueGame, settings, now: datetime) -> list[Candidate]:
    if game.start and game.start - now < timedelta(minutes=settings.min_minutes_before_start):
        return []
    ref = reference(game, vg.venue, settings.entry_reference)
    if not ref:
        return []
    source, fair = ref
    sf = sharp_fair(game)
    out = []
    for key, q in vg.outcomes.items():
        if key not in fair:
            continue
        for side in settings.entry_sides:
            asks = q.yes_asks if side == "yes" else q.no_asks
            if side == "no" and not asks and not game.three_way and len(vg.outcomes) == 2:
                continue  # NBA: NO on a team is YES on the other, already evaluated
            if not asks or not (settings.min_price <= asks[0][0] <= settings.max_price):
                continue
            p = fair[key] if side == "yes" else 1.0 - fair[key]
            spf = None if sf is None or key not in sf else (sf[key] if side == "yes" else 1.0 - sf[key])
            fill = fill_for_stake(asks, settings.stake_usd, vg.fees)
            if not fill.shares or (not fill.complete and not settings.allow_partial):
                continue
            c = Candidate(vg.venue, q, side, fill, p, source, spf)
            if c.edge >= settings.min_edge:
                out.append(c)
    return out
