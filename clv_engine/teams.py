"""Team-name normalization and alias resolution for cross-venue matching."""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

ALIAS_FILE = Path(__file__).parent / "data" / "team_aliases.json"
FILLER = {"fc", "cf", "cd", "club", "sc", "ac", "afc", "ud", "rcd", "sd", "ssc", "sl", "the", "de", "del", "la", "el", "cp", "as", "rc", "vfb", "losc"}


TRANSLIT = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "ß": "ss", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ı": "i"})


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", (name or "").translate(TRANSLIT))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [t for t in text.split() if t not in FILLER]
    return " ".join(tokens)


@lru_cache(maxsize=1)
def _tables() -> dict[str, dict[str, str]]:
    raw = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for league, teams in raw.items():
        if league.startswith("_"):
            continue
        table: dict[str, str] = {}
        for canon, aliases in teams.items():
            table[normalize(canon.replace("-", " "))] = canon
            for alias in aliases:
                n = normalize(alias)
                if n:
                    table.setdefault(n, canon)
        out[league] = table
    return out


def canonical(name: str, league: str) -> str:
    """Return a canonical team id, or "x:<normalized>" when unknown."""
    norm = normalize(name)
    table = _tables().get(league, {})
    if norm in table:
        return table[norm]
    tokens = set(norm.split())
    if tokens:
        multi, single = set(), set()
        for alias, canon in table.items():
            at = set(alias.split())
            if len(at) >= 2 and at <= tokens:
                multi.add(canon)  # "santos laguna" inside "club santos laguna"
            elif len(at) >= 2 and tokens <= at and len(norm) >= 4:
                multi.add(canon)  # "vallecano" inside "rayo vallecano"
            elif len(at) == 1 and len(alias) >= 5 and at <= tokens:
                single.add(canon)  # "brugge" inside "brugge kv"
        if len(multi) == 1:
            return multi.pop()
        if not multi and len(single) == 1:
            return single.pop()
    return f"x:{norm}"


def same_team(a: str, b: str) -> bool:
    """Compare canonical ids; unknown ids fall back to token containment."""
    if a == b:
        return True
    if a.startswith("x:") or b.startswith("x:"):
        ta, tb = set(a.removeprefix("x:").split()), set(b.removeprefix("x:").split())
        ta = {t for t in ta if len(t) > 2}
        tb = {t for t in tb if len(t) > 2}
        if ta and tb and (ta <= tb or tb <= ta):
            return True
    return False


def split_title(title: str) -> list[str]:
    """'Querétaro FC vs. CF Monterrey' -> ['Querétaro FC', 'CF Monterrey']."""
    t = re.split(r"\s+(?:vs\.?|v\.?|@|at)\s+", title.strip(), maxsplit=1, flags=re.I)
    if len(t) == 2:
        right = re.split(r"\s+-\s+", t[1], maxsplit=1)[0]
        return [t[0].strip(), right.strip()]
    return []
