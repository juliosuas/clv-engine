"""SQLite paper ledger. Every row is simulated; no money ever moves."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .util import iso, parse_datetime, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    game_key TEXT PRIMARY KEY,
    league TEXT NOT NULL,
    teams TEXT NOT NULL,            -- json [id, id] sorted
    labels TEXT,                    -- json {id: label}
    start_time TEXT,
    start_source TEXT,
    refs TEXT,                      -- json {venue: ref}
    first_seen TEXT,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    venue TEXT NOT NULL,            -- polymarket | kalshi | <soft book key>
    league TEXT NOT NULL,
    game_key TEXT NOT NULL,
    game_label TEXT,
    event_ref TEXT,
    market_ref TEXT,
    outcome_key TEXT NOT NULL,
    outcome_label TEXT,
    side TEXT NOT NULL,             -- yes | no (soft books: yes)
    start_time TEXT,
    stake REAL NOT NULL,
    shares REAL NOT NULL,
    best_ask REAL,
    avg_price REAL NOT NULL,
    fee REAL NOT NULL,
    fee_model TEXT,
    effective_price REAL NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0,
    depth_levels TEXT,              -- json [[price, shares], ...]
    ref_source TEXT NOT NULL,       -- pinnacle | consensus
    fair_entry REAL NOT NULL,       -- reference prob used for the entry rule
    sharp_fair_entry REAL,          -- pinnacle de-vigged prob at entry (NULL if no feed)
    edge_entry REAL NOT NULL,       -- fair_entry / effective_price - 1
    sharp_fair_close REAL,
    venue_mid_close REAL,
    clv_pct REAL,                   -- sharp_fair_close / effective_price - 1 (after fees)
    clv_pct_raw REAL,               -- sharp_fair_close / avg_price - 1 (before fees)
    venue_clv_pct REAL,             -- venue_mid_close / avg_price - 1
    clv_status TEXT NOT NULL DEFAULT 'pending_close',   -- pending_close | pending_sharp | no_close | done
    settle_venue TEXT,
    settle_ref TEXT,
    status TEXT NOT NULL DEFAULT 'open',                -- open | settled
    result_value REAL,              -- settlement value per share of the side held (0..1)
    pnl REAL,
    settled_at TEXT
);
CREATE INDEX IF NOT EXISTS entries_game ON entries(game_key, venue, outcome_key, side);
CREATE TABLE IF NOT EXISTS closing (
    game_key TEXT NOT NULL,
    outcome_key TEXT NOT NULL,
    source TEXT NOT NULL,           -- pinnacle | consensus | polymarket | kalshi
    value REAL NOT NULL,            -- fair prob (pinnacle/consensus) or venue mid
    captured_at TEXT NOT NULL,
    PRIMARY KEY (game_key, outcome_key, source)
);
CREATE TABLE IF NOT EXISTS quotes (
    ts TEXT NOT NULL,
    game_key TEXT NOT NULL,
    source TEXT NOT NULL,
    outcome_key TEXT NOT NULL,
    best_ask REAL,
    best_bid REAL,
    ask_depth REAL,
    fair REAL
);
CREATE INDEX IF NOT EXISTS quotes_k ON quotes(game_key, source, outcome_key, ts);
CREATE TABLE IF NOT EXISTS arbs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,             -- multi_outcome | cross_venue
    league TEXT,
    game_key TEXT,
    legs TEXT NOT NULL,             -- json
    shares REAL, cost REAL, fees REAL, profit REAL, roi REAL
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    last_ts TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (
    day TEXT PRIMARY KEY,           -- UTC date YYYY-MM-DD
    requests INTEGER NOT NULL DEFAULT 0,   -- /odds calls that reached the provider
    credits REAL NOT NULL DEFAULT 0,       -- credits spent today (x-requests-last when sent, else the estimate)
    skipped INTEGER NOT NULL DEFAULT 0,    -- polls skipped because of the budget
    remaining INTEGER,                     -- last x-requests-remaining seen (quota period, not necessarily daily)
    used INTEGER,                          -- last x-requests-used seen
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS missed_closes (
    league TEXT NOT NULL,
    round_start TEXT NOT NULL,             -- first kickoff of the round whose close poll could not be made
    round_end TEXT NOT NULL,               -- last kickoff grouped into the round
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (league, round_start)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    seconds REAL,
    games INTEGER, venue_games INTEGER, sharp_games INTEGER,
    entries INTEGER, arbs INTEGER, errors INTEGER,
    note TEXT
);
"""


# Sensitivity closes: the same Pinnacle sample de-vigged with each method, stored next to the primary
# ('pinnacle', DEVIG_METHOD) close so reports can show CLV under both (see docs/GRADUATION.md).
ALT_CLOSE_METHODS = ("power", "multiplicative")


def alt_close_source(method: str) -> str:
    return f"pinnacle_{method}"


class Ledger:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- games -----------------------------------------------------------
    def resolve_game_key(self, league: str, teams: list[str], start: datetime | None, proposed: str, window_hours: float) -> str:
        tj = json.dumps(sorted(teams))
        rows = self.conn.execute("SELECT game_key, start_time FROM games WHERE league=? AND teams=?", (league, tj)).fetchall()
        for r in rows:
            st = parse_datetime(r["start_time"])
            if start is None or st is None or abs((st - start).total_seconds()) <= window_hours * 3600:
                return r["game_key"]
        return proposed

    def upsert_game(self, key: str, league: str, teams: list[str], labels: dict, start: datetime | None, start_source: str, refs: dict) -> None:
        now = iso(utc_now())
        row = self.conn.execute("SELECT refs, start_source FROM games WHERE game_key=?", (key,)).fetchone()
        if row:
            merged = {**json.loads(row["refs"] or "{}"), **refs}
            rank = {"sharp": 0, "polymarket": 1, "kalshi": 2}
            keep_start = rank.get(row["start_source"] or "", 9) < rank.get(start_source, 9)
            if keep_start:
                self.conn.execute("UPDATE games SET refs=?, labels=?, last_seen=? WHERE game_key=?", (json.dumps(merged), json.dumps(labels), now, key))
            else:
                self.conn.execute("UPDATE games SET refs=?, labels=?, start_time=?, start_source=?, last_seen=? WHERE game_key=?",
                                  (json.dumps(merged), json.dumps(labels), iso(start), start_source, now, key))
        else:
            self.conn.execute("INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?)",
                              (key, league, json.dumps(sorted(teams)), json.dumps(labels), iso(start), start_source, json.dumps(refs), now, now))
        self.conn.commit()

    def game(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM games WHERE game_key=?", (key,)).fetchone()

    # ---- entries ---------------------------------------------------------
    def count_entries(self, game_key: str, venue: str, outcome_key: str, side: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM entries WHERE game_key=? AND venue=? AND outcome_key=? AND side=?",
                                 (game_key, venue, outcome_key, side)).fetchone()[0]

    def count_entries_any_venue(self, game_key: str, outcome_key: str, side: str) -> int:
        """Entries on the real venues (not soft books) for one game/outcome/side, whatever the venue."""
        return self.conn.execute("SELECT COUNT(*) FROM entries WHERE game_key=? AND outcome_key=? AND side=? AND venue IN ('polymarket','kalshi')",
                                 (game_key, outcome_key, side)).fetchone()[0]

    def add_entry(self, **e: Any) -> int:
        e.setdefault("created_at", iso(utc_now()))
        if isinstance(e.get("depth_levels"), (list, tuple)):
            e["depth_levels"] = json.dumps([[round(p, 6), round(s, 6)] for p, s in e["depth_levels"]])
        cols = ",".join(e)
        cur = self.conn.execute(f"INSERT INTO entries ({cols}) VALUES ({','.join('?' * len(e))})", tuple(e.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def entries(self, where: str = "1=1", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM entries WHERE {where} ORDER BY id", tuple(params)).fetchall()

    def update_entry(self, entry_id: int, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE entries SET {sets} WHERE id=?", (*fields.values(), entry_id))
        self.conn.commit()

    # ---- closing lines ---------------------------------------------------
    def capture_closing(self, game_key: str, outcome_key: str, source: str, value: float, at: datetime | None = None) -> None:
        self.conn.execute(
            "INSERT INTO closing VALUES (?,?,?,?,?) ON CONFLICT(game_key, outcome_key, source) DO UPDATE SET value=excluded.value, captured_at=excluded.captured_at",
            (game_key, outcome_key, source, float(value), iso(at or utc_now())),
        )
        self.conn.commit()

    def closing(self, game_key: str, outcome_key: str, source: str) -> float | None:
        r = self.conn.execute("SELECT value FROM closing WHERE game_key=? AND outcome_key=? AND source=?", (game_key, outcome_key, source)).fetchone()
        return None if r is None else float(r[0])

    def closing_row(self, game_key: str, outcome_key: str, source: str) -> tuple[float, datetime | None] | None:
        """(value, captured_at) of a stored closing line."""
        r = self.conn.execute("SELECT value, captured_at FROM closing WHERE game_key=? AND outcome_key=? AND source=?",
                              (game_key, outcome_key, source)).fetchone()
        return None if r is None else (float(r["value"]), parse_datetime(r["captured_at"]))

    def pending_kickoffs(self, league: str) -> list[datetime]:
        """Kickoff times of games that still have entries without a Pinnacle close (not yet 'done')."""
        rows = self.conn.execute("SELECT DISTINCT start_time FROM entries WHERE league=? AND clv_status != 'done' AND start_time IS NOT NULL",
                                 (league,)).fetchall()
        return sorted(t for t in (parse_datetime(r[0]) for r in rows) if t is not None)

    # ---- Odds API credit usage ---------------------------------------------
    def usage(self, day: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM api_usage WHERE day=?", (day,)).fetchone()
        if r is None:
            return {"day": day, "requests": 0, "credits": 0.0, "skipped": 0, "remaining": None, "used": None}
        return dict(r)

    def usage_add(self, day: str, now: datetime, credits: float = 0.0, requests: int = 0, skipped: int = 0,
                  remaining: int | None = None, used: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO api_usage (day, requests, credits, skipped, remaining, used, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET requests=requests+excluded.requests, credits=credits+excluded.credits, "
            "skipped=skipped+excluded.skipped, remaining=COALESCE(excluded.remaining, remaining), "
            "used=COALESCE(excluded.used, used), updated_at=excluded.updated_at",
            (day, requests, credits, skipped, remaining, used, iso(now)),
        )
        self.conn.commit()

    def month_usage(self, month: str) -> dict[str, Any]:
        """Totals for a UTC calendar month 'YYYY-MM' plus the latest x-requests-remaining/used seen in it."""
        like = month + "-%"
        r = self.conn.execute("SELECT COALESCE(SUM(credits),0), COALESCE(SUM(requests),0), COALESCE(SUM(skipped),0) FROM api_usage WHERE day LIKE ?", (like,)).fetchone()
        h = self.conn.execute("SELECT remaining, used FROM api_usage WHERE day LIKE ? AND remaining IS NOT NULL ORDER BY day DESC LIMIT 1", (like,)).fetchone()
        return {"month": month, "credits": float(r[0]), "requests": int(r[1]), "skipped": int(r[2]),
                "remaining": None if h is None else h[0], "used": None if h is None else h[1]}

    # ---- close polls the credit budget could not afford (entries of these rounds are marked no_close) ----
    def add_missed_close(self, league: str, round_start: datetime, round_end: datetime, now: datetime, reason: str) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO missed_closes VALUES (?,?,?,?,?)", (league, iso(round_start), iso(round_end), iso(now), reason))
        self.conn.commit()
        return cur.rowcount > 0

    def clear_missed_close(self, league: str, round_start: datetime) -> None:
        self.conn.execute("DELETE FROM missed_closes WHERE league=? AND round_start=?", (league, iso(round_start)))
        self.conn.commit()

    def missed_close_for(self, league: str, kickoff: datetime, slack_minutes: float = 1.0) -> dict[str, Any] | None:
        """The missed-close record whose round contains `kickoff`, if any."""
        for r in self.conn.execute("SELECT * FROM missed_closes WHERE league=?", (league,)).fetchall():
            a, b = parse_datetime(r["round_start"]), parse_datetime(r["round_end"])
            if a and b and a - timedelta(minutes=slack_minutes) <= kickoff <= b + timedelta(minutes=slack_minutes):
                return dict(r)
        return None

    def missed_closes(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM missed_closes ORDER BY round_start").fetchall()]

    def last_known_remaining(self) -> int | None:
        r = self.conn.execute("SELECT remaining FROM api_usage WHERE remaining IS NOT NULL ORDER BY day DESC LIMIT 1").fetchone()
        return None if r is None else int(r[0])

    def all_quotes(self) -> list[dict[str, Any]]:
        """Stored book ticks of every game that has an entry (read-only; used by the audit's spread split)."""
        rows = self.conn.execute("SELECT q.* FROM quotes q WHERE q.game_key IN (SELECT DISTINCT game_key FROM entries) ORDER BY q.ts").fetchall()
        return [dict(r) for r in rows]

    # ---- quotes (price history with dedupe, pattern from polymarket-desk api/market.py) ----
    def add_quote(self, game_key: str, source: str, outcome_key: str, best_ask: float | None, best_bid: float | None, ask_depth: float | None,
                  fair: float | None, min_seconds: int, now: datetime | None = None) -> bool:
        now = now or utc_now()
        last = self.conn.execute(
            "SELECT ts, best_ask, best_bid, fair FROM quotes WHERE game_key=? AND source=? AND outcome_key=? ORDER BY ts DESC LIMIT 1",
            (game_key, source, outcome_key),
        ).fetchone()
        if last:
            unchanged = (last["best_ask"], last["best_bid"], last["fair"]) == (best_ask, best_bid, None if fair is None else round(fair, 6))
            age = (now - parse_datetime(last["ts"])).total_seconds()
            if unchanged and age < min_seconds:
                return False
        self.conn.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?)",
                          (iso(now), game_key, source, outcome_key, best_ask, best_bid, ask_depth, None if fair is None else round(fair, 6)))
        return True

    def commit(self) -> None:
        self.conn.commit()

    # ---- arbs / runs -----------------------------------------------------
    def add_arb(self, kind: str, league: str, game_key: str, legs: list[dict], shares: float, cost: float, fees: float, profit: float, roi: float) -> None:
        self.conn.execute("INSERT INTO arbs (ts, kind, league, game_key, legs, shares, cost, fees, profit, roi) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (iso(utc_now()), kind, league, game_key, json.dumps(legs), shares, cost, fees, profit, roi))
        self.conn.commit()

    def add_run(self, **r: Any) -> None:
        r.setdefault("ts", iso(utc_now()))
        cols = ",".join(r)
        self.conn.execute(f"INSERT INTO runs ({cols}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
        self.conn.commit()

    # ---- alert dedupe ----------------------------------------------------
    def alert_due(self, key: str, cooldown_seconds: int, now: datetime | None = None) -> bool:
        now = now or utc_now()
        r = self.conn.execute("SELECT last_ts FROM alerts WHERE key=?", (key,)).fetchone()
        if r and (now - parse_datetime(r["last_ts"])) < timedelta(seconds=cooldown_seconds):
            return False
        return True

    def alert_mark(self, key: str, payload: dict, now: datetime | None = None) -> None:
        self.conn.execute(
            "INSERT INTO alerts VALUES (?,?,1,?) ON CONFLICT(key) DO UPDATE SET last_ts=excluded.last_ts, count=count+1, payload=excluded.payload",
            (key, iso(now or utc_now()), json.dumps(payload, default=str)),
        )
        self.conn.commit()
