"""Cross-venue game matching: same league, same pair of teams, kickoff within a window."""

from __future__ import annotations

from datetime import datetime

from .models import MatchedGame, SharpGame, VenueGame
from .teams import same_team

VENUE_PRIORITY = ("polymarket", "kalshi")


def teams_match(a: list[str], b: list[str]) -> bool:
    if len(a) != 2 or len(b) != 2:
        return False
    return (same_team(a[0], b[0]) and same_team(a[1], b[1])) or (same_team(a[0], b[1]) and same_team(a[1], b[0]))


def times_close(a: datetime | None, b: datetime | None, window_hours: float) -> bool:
    if a is None or b is None:
        return True  # team pair alone (within the lookahead) is the fallback
    return abs((a - b).total_seconds()) <= window_hours * 3600


def game_key(league: str, teams: list[str], start: datetime | None) -> str:
    day = start.strftime("%Y%m%d") if start else "nodate"
    return f"{league}:{day}:{'|'.join(sorted(teams))}"


def _align_outcomes(vg: VenueGame, anchor_teams: list[str]) -> None:
    """Re-key a venue's outcomes onto the anchor's canonical team ids."""
    remap = {}
    for key in list(vg.outcomes):
        for t in anchor_teams:
            if key != t and same_team(key, t):
                remap[key] = t
    for old, new in remap.items():
        q = vg.outcomes.pop(old)
        q.key = new
        vg.outcomes[new] = q
    vg.teams = [next((t for t in anchor_teams if same_team(x, t)), x) for x in vg.teams]


def match_games(league: str, three_way: bool, venue_games: list[VenueGame], sharp_games: list[SharpGame], window_hours: float = 30) -> list[MatchedGame]:
    groups: list[MatchedGame] = []
    for sg in sharp_games:
        groups.append(MatchedGame(league=league, key="", teams=sg.teams, labels={sg.home: sg.home_label, sg.away: sg.away_label},
                                  start=sg.start, three_way=three_way, sharp=sg))
    ordered = sorted(venue_games, key=lambda g: VENUE_PRIORITY.index(g.venue) if g.venue in VENUE_PRIORITY else 99)
    for vg in ordered:
        target = None
        for g in groups:
            if vg.venue in g.venues:
                continue
            if teams_match(g.teams, vg.teams) and times_close(g.start, vg.start, window_hours):
                target = g
                break
        if target is None:
            target = MatchedGame(league=league, key="", teams=list(vg.teams), labels=dict(zip(vg.teams, vg.team_labels)),
                                 start=vg.start, three_way=three_way)
            groups.append(target)
        _align_outcomes(vg, target.teams)
        target.venues[vg.venue] = vg
        for t, lab in zip(vg.teams, vg.team_labels):
            target.labels.setdefault(t, lab)
        # kickoff precedence: sharp commence_time > polymarket gameStartTime > kalshi hint
        if target.sharp is None and vg.venue == "polymarket" and vg.start:
            target.start = vg.start
    for g in groups:
        g.key = game_key(league, g.teams, g.start)
    return groups
