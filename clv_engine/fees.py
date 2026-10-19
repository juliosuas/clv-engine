"""Taker fee models (paper simulation only).

Polymarket (https://docs.polymarket.com/trading/fees, checked 2026-10-18):
    fee = C * feeRate * p * (1 - p), takers only, rounded to 5 decimals (USDC).
    Sports feeRate = 0.05. Each Gamma market also carries `feeSchedule.rate`,
    which we prefer when present.

Kalshi (General Trading Fees Table, https://kalshi.com/docs/kalshi-fee-schedule.pdf,
referenced by the API `fee_type` docs at
https://docs.kalshi.com/api-reference/market/get-series):
    taker fee = round_up(0.07 * C * P * (1 - P)) to the next cent, times the
    series `fee_multiplier`. `fee_type` quadratic / quadratic_with_maker_fees
    share the same taker formula (maker fees only apply to resting orders,
    which a paper taker never is). Kalshi's fee-rounding doc
    (https://docs.kalshi.com/getting_started/fee_rounding) describes the
    modern mechanics: trade fee ceil'd to $0.000001 plus a rounding fee that
    brings a non-direct member's balance back to the cent; for one order
    that is equivalent to rounding the total up to the cent, which is what
    we model by default (KALSHI_FEE_ROUNDING=cent). Use "micro" for
    direct-member $0.0001 precision.
"""

from __future__ import annotations

import math

POLYMARKET_SPORTS_RATE = 0.05
KALSHI_TAKER_RATE = 0.07


def polymarket_fee(shares: float, price: float, rate: float = POLYMARKET_SPORTS_RATE) -> float:
    if shares <= 0 or rate <= 0:
        return 0.0
    fee = shares * rate * price * (1.0 - price)
    return round(fee, 5)


def _ceil_to(value: float, step: float) -> float:
    # tolerate float noise: 0.07*100*0.5*0.5 = 1.7500000000000002
    return math.ceil(round(value / step, 9)) * step


def kalshi_fee_raw(shares: float, price: float, multiplier: float = 1.0) -> float:
    if shares <= 0:
        return 0.0
    return KALSHI_TAKER_RATE * multiplier * shares * price * (1.0 - price)


def kalshi_fee(shares: float, price: float, multiplier: float = 1.0, rounding: str = "cent") -> float:
    raw = kalshi_fee_raw(shares, price, multiplier)
    if raw <= 0:
        return 0.0
    step = 0.01 if rounding == "cent" else 0.0001
    return round(_ceil_to(raw, step), 6)


class FeeModel:
    """Fee for a multi-level taker fill. `levels` = [(price, shares), ...]."""

    def __init__(self, venue: str, rate: float | None = None, multiplier: float = 1.0, rounding: str = "cent"):
        self.venue = venue
        self.rate = POLYMARKET_SPORTS_RATE if rate is None else rate
        self.multiplier = multiplier
        self.rounding = rounding

    def fill_fee(self, levels: list[tuple[float, float]]) -> float:
        if self.venue == "polymarket":
            return round(sum(polymarket_fee(q, p, self.rate) for p, q in levels), 5)
        if self.venue == "kalshi":
            raw = sum(kalshi_fee_raw(q, p, self.multiplier) for p, q in levels)
            if raw <= 0:
                return 0.0
            step = 0.01 if self.rounding == "cent" else 0.0001
            return round(_ceil_to(raw, step), 6)
        return 0.0

    def describe(self) -> str:
        if self.venue == "polymarket":
            return f"polymarket taker C*{self.rate}*p(1-p)"
        return f"kalshi taker ceil({KALSHI_TAKER_RATE}*{self.multiplier}*C*P(1-P), {self.rounding})"
