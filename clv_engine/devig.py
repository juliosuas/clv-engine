"""Odds conversion and de-vig (remove the bookmaker margin).

Methods (all take decimal odds for a complete, mutually exclusive market,
e.g. 1X2 or a two-way moneyline, and return fair probabilities summing to 1):

* multiplicative ("basic"/proportional): p_i = (1/o_i) / sum(1/o_j).
  Spreads the margin proportionally; tends to overrate longshots.
* power: find k so that sum((1/o_i)^k) = 1; p_i = (1/o_i)^k.
  Takes more margin from longshots (favourite-longshot bias). Default.
* shin: Shin (1992/1993) insider-trading model. Solve for z in [0, 0.5)
  such that sum(p_i) = 1 with
      p_i = (sqrt(z^2 + 4(1-z) * pi_i^2 / S) - z) / (2(1-z)),
  pi_i = 1/o_i, S = sum(pi). Also shades longshots.

See e.g. Clarke, Kovalchik & Ingram (2017), "Adjusting bookmaker's odds to
allow for overround", American Journal of Sports Science.
"""

from __future__ import annotations

import math

METHODS = ("multiplicative", "power", "shin")


def american_to_decimal(american: float) -> float:
    if american >= 100:
        return 1.0 + american / 100.0
    if american <= -100:
        return 1.0 + 100.0 / abs(american)
    raise ValueError(f"invalid American odds: {american}")


def implied(odds: list[float]) -> list[float]:
    if not odds or any(o <= 1.0 for o in odds):
        raise ValueError(f"decimal odds must be > 1: {odds}")
    return [1.0 / o for o in odds]


def overround(odds: list[float]) -> float:
    return sum(implied(odds)) - 1.0


def multiplicative(odds: list[float]) -> list[float]:
    pi = implied(odds)
    s = sum(pi)
    return [p / s for p in pi]


def power(odds: list[float], tol: float = 1e-12) -> list[float]:
    pi = implied(odds)
    if abs(sum(pi) - 1.0) < tol:
        return pi
    lo, hi = 0.01, 10.0
    for _ in range(200):
        k = (lo + hi) / 2
        s = sum(p**k for p in pi)
        if s > 1.0:
            lo = k  # need larger k to shrink
        else:
            hi = k
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    out = [p**k for p in pi]
    s = sum(out)
    return [p / s for p in out]


def shin(odds: list[float], tol: float = 1e-12) -> list[float]:
    pi = implied(odds)
    s = sum(pi)
    if s <= 1.0:
        return [p / s for p in pi]

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4 * (1 - z) * p * p / s) - z) / (2 * (1 - z)) for p in pi]

    lo, hi = 0.0, 0.5
    for _ in range(200):
        z = (lo + hi) / 2
        total = sum(probs(z))
        if total > 1.0:
            lo = z
        else:
            hi = z
        if hi - lo < tol:
            break
    out = probs((lo + hi) / 2)
    t = sum(out)
    return [p / t for p in out]


def devig(odds: list[float], method: str = "power") -> list[float]:
    method = (method or "power").lower()
    if method in ("multiplicative", "basic", "proportional"):
        return multiplicative(odds)
    if method == "power":
        return power(odds)
    if method == "shin":
        return shin(odds)
    raise ValueError(f"unknown de-vig method {method!r}; use one of {METHODS}")
