"""Sharp (Pinnacle) and soft-book reference adapters, selected by SHARP_SOURCE."""

from __future__ import annotations

from .base import NoSharpSource, SharpSource
from .theoddsapi import TheOddsApiSource


def make_sharp_source(settings, http) -> SharpSource:
    src = (settings.sharp_source or "none").lower()
    if src == "theoddsapi":
        if not settings.odds_api_key:
            return NoSharpSource(reason="SHARP_SOURCE=theoddsapi but ODDS_API_KEY is not set")
        return TheOddsApiSource(http, settings.odds_api_key, settings)
    return NoSharpSource(reason=f"SHARP_SOURCE={src}")


__all__ = ["SharpSource", "NoSharpSource", "TheOddsApiSource", "make_sharp_source"]
