"""Plain data containers shared across venues, sharp sources and the engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .fees import FeeModel
from .orderbook import Level, best, sort_asks, sort_bids

DRAW = "draw"


@dataclass
class OutcomeQuote:
    """One mutually exclusive game result on one venue (team win or draw)."""

    key: str  # canonical team id or "draw"
    label: str
    ref: str  # polymarket token id / kalshi market ticker
    yes_asks: list[Level] = field(default_factory=list)
    yes_bids: list[Level] = field(default_factory=list)
    no_asks: list[Level] = field(default_factory=list)  # buying NO on this outcome
    no_ref: str | None = None
    market_ref: str | None = None  # polymarket conditionId / kalshi ticker
    display_price: float | None = None  # gamma/kalshi top-of-book hint (not executable)

    def __post_init__(self) -> None:
        self.yes_asks = sort_asks(self.yes_asks)
        self.yes_bids = sort_bids(self.yes_bids)
        self.no_asks = sort_asks(self.no_asks)

    @property
    def best_ask(self) -> float | None:
        return best(self.yes_asks, True)

    @property
    def best_bid(self) -> float | None:
        return best(self.yes_bids, False)

    @property
    def mid(self) -> float | None:
        a, b = self.best_ask, self.best_bid
        if a is not None and b is not None:
            return (a + b) / 2
        return a if a is not None else b


@dataclass
class VenueGame:
    venue: str  # "polymarket" | "kalshi"
    league: str
    ref: str  # event slug / event ticker
    title: str
    teams: list[str]  # canonical team ids (order as listed by the venue)
    team_labels: list[str]
    start: datetime | None
    outcomes: dict[str, OutcomeQuote]
    fees: FeeModel
    three_way: bool
    url: str | None = None

    @property
    def exhaustive(self) -> bool:
        need = len(self.teams) + (1 if self.three_way else 0)
        return len(self.outcomes) == need and all(o.yes_asks for o in self.outcomes.values())


@dataclass
class BookLine:
    """De-vigged line from one bookmaker (sharp = Pinnacle, or a soft book)."""

    book: str
    odds: dict[str, float]  # outcome key -> decimal odds
    fair: dict[str, float]  # outcome key -> fair probability
    last_update: datetime | None = None


@dataclass
class SharpGame:
    league: str
    ref: str  # provider event id
    home: str
    away: str
    home_label: str
    away_label: str
    start: datetime | None
    sharp: BookLine | None
    soft: dict[str, BookLine] = field(default_factory=dict)

    @property
    def teams(self) -> list[str]:
        return [self.home, self.away]


@dataclass
class MatchedGame:
    league: str
    key: str
    teams: list[str]
    labels: dict[str, str]
    start: datetime | None
    three_way: bool
    venues: dict[str, VenueGame] = field(default_factory=dict)
    sharp: SharpGame | None = None
