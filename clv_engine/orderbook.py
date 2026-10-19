"""Order book levels and depth-aware taker fills (simulation only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .fees import FeeModel

Level = tuple[float, float]  # (price, shares)


def sort_asks(levels: list[Level]) -> list[Level]:
    return sorted(((p, s) for p, s in levels if 0 < p < 1 and s > 0), key=lambda x: x[0])


def sort_bids(levels: list[Level]) -> list[Level]:
    return sorted(((p, s) for p, s in levels if 0 < p < 1 and s > 0), key=lambda x: -x[0])


def complement(bids: list[Level]) -> list[Level]:
    """Bids on the opposite side of a binary contract are asks on this side at 1 - p."""
    return sort_asks([(round(1.0 - p, 6), s) for p, s in bids])


@dataclass
class Fill:
    shares: float = 0.0
    notional: float = 0.0
    fee: float = 0.0
    levels: list[Level] = field(default_factory=list)
    complete: bool = False

    @property
    def avg_price(self) -> float:
        return self.notional / self.shares if self.shares else 0.0

    @property
    def cost(self) -> float:
        return self.notional + self.fee

    @property
    def effective_price(self) -> float:
        """All-in cost per share (price + fee)."""
        return self.cost / self.shares if self.shares else 0.0


def fill_for_stake(asks: list[Level], stake: float, fees: FeeModel) -> Fill:
    """Walk the asks spending up to `stake` dollars of notional (fee on top)."""
    fill = Fill()
    remaining = stake
    for price, size in sort_asks(asks):
        if remaining <= 1e-9:
            break
        take = min(size, remaining / price)
        if take <= 0:
            continue
        fill.levels.append((price, take))
        fill.shares += take
        fill.notional += take * price
        remaining -= take * price
    fill.complete = remaining <= 1e-6
    fill.fee = fees.fill_fee(fill.levels)
    return fill


def fill_for_shares(asks: list[Level], shares: float, fees: FeeModel) -> Fill:
    fill = Fill()
    remaining = shares
    for price, size in sort_asks(asks):
        if remaining <= 1e-9:
            break
        take = min(size, remaining)
        fill.levels.append((price, take))
        fill.shares += take
        fill.notional += take * price
        remaining -= take
    fill.complete = remaining <= 1e-9
    fill.fee = fees.fill_fee(fill.levels)
    return fill


def depth(asks: list[Level]) -> float:
    return sum(s for _, s in asks)


def best(levels: list[Level], asks: bool = True) -> float | None:
    ordered = sort_asks(levels) if asks else sort_bids(levels)
    return ordered[0][0] if ordered else None
