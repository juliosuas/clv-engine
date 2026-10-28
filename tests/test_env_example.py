"""systemd EnvironmentFile and docker env_file keep everything after "=" verbatim.

An inline comment (``KEY=value   # note``) silently becomes part of the value, so
every env example must keep comments on their own lines.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV_FILES = sorted({*ROOT.glob(".env.example"), *ROOT.glob("**/*.env.example")} - set(ROOT.glob(".venv/**/*")))
KEY_LINE = re.compile(r"^[A-Z][A-Z0-9_]*=")


def value_lines(path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("#"):
            yield n, line


def test_env_examples_exist():
    assert ROOT / ".env.example" in ENV_FILES


@pytest.mark.parametrize("path", ENV_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_env_example_has_no_inline_comments(path):
    bad = []
    for n, line in value_lines(path):
        if not KEY_LINE.match(line):
            bad.append(f"{path.name}:{n}: not a plain KEY=value line: {line!r}")
        elif " #" in line or "\t#" in line or line != line.rstrip():
            bad.append(f"{path.name}:{n}: inline comment or trailing whitespace: {line!r}")
    assert not bad, "\n".join(bad)


def test_readme_env_snippets_have_no_inline_comments():
    bad = [line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
           if KEY_LINE.match(line.strip()) and " #" in line]
    assert not bad, bad


def test_env_example_loads_clean_values(monkeypatch):
    """Loading the example verbatim (as systemd would) gives parseable values."""
    from clv_engine.config import Settings
    for _, line in value_lines(ROOT / ".env.example"):
        key, _, value = line.partition("=")
        monkeypatch.setenv(key, value)
    s = Settings.from_env(dotenv=None)
    assert s.sharp_source == "none" and s.poll_seconds == 120
    assert s.leagues == ["nba", "nfl", "epl", "laliga", "ligamx", "ucl"]
    assert s.excluded_leagues == ["ucl"] and s.excluded_venues == ["kalshi"]
    assert s.min_edge == 0.05 and s.require_pinnacle_ref is True and s.min_depth_usd == 250
    assert s.odds_api_daily_budget == 1000 and s.close_capture_minutes == 30


def test_env_example_matches_code_defaults(monkeypatch):
    """Every KEY in .env.example that Settings reads must equal the dataclass default (no drift)."""
    from dataclasses import asdict

    from clv_engine.config import Settings
    for _, line in value_lines(ROOT / ".env.example"):
        key, _, value = line.partition("=")
        monkeypatch.setenv(key, value)
    from_example, defaults = asdict(Settings.from_env(dotenv=None)), asdict(Settings())
    for key in ("excluded_leagues", "excluded_venues", "min_edge", "require_pinnacle_ref", "min_depth_usd", "depth_tolerance",
                "odds_api_daily_budget", "odds_api_close_reserve", "odds_api_min_remaining", "max_sharp_age_minutes", "close_max_age_minutes",
                "sharp_near_poll_seconds", "sharp_idle_poll_seconds", "near_window_minutes", "idle_after_minutes", "leagues",
                "grad_min_games", "grad_ci_level", "grad_min_venues", "grad_min_leagues", "grad_max_league_share"):
        assert from_example[key] == defaults[key], key
