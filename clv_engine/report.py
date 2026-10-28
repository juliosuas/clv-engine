"""Ledger reporting: CLI summary and weekly markdown report."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ledger import Ledger
from .util import parse_datetime, utc_now


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name), name
    except Exception:  # noqa: BLE001 - tzdata missing
        return timezone.utc, "UTC"


def _local(ts: str | None, tz) -> str:
    dt = parse_datetime(ts)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else "—"


def _agg(rows) -> dict:
    n = len(rows)
    settled = [r for r in rows if r["status"] == "settled"]
    wins = [r for r in settled if (r["result_value"] or 0) >= 0.999]
    staked = sum(r["stake"] + r["fee"] for r in rows)
    settled_cost = sum(r["stake"] + r["fee"] for r in settled)
    pnl = sum(r["pnl"] or 0 for r in settled)
    clv = [r["clv_pct"] for r in rows if r["clv_pct"] is not None]
    vclv = [r["venue_clv_pct"] for r in rows if r["venue_clv_pct"] is not None]
    return {
        "entries": n,
        "open": n - len(settled),
        "settled": len(settled),
        "hit_rate": (len(wins) / len(settled)) if settled else None,
        "staked": staked,
        "pnl": pnl,
        "roi": (pnl / settled_cost) if settled_cost else None,
        "avg_edge": (sum(r["edge_entry"] for r in rows) / n) if n else None,
        "clv_n": len(clv),
        "avg_clv": (sum(clv) / len(clv)) if clv else None,
        "clv_beat": (sum(1 for c in clv if c > 0) / len(clv)) if clv else None,
        "venue_clv_n": len(vclv),
        "avg_venue_clv": (sum(vclv) / len(vclv)) if vclv else None,
        "pending_sharp": sum(1 for r in rows if r["clv_status"] == "pending_sharp"),
        "pending_close": sum(1 for r in rows if r["clv_status"] == "pending_close"),
    }


def _pct(x) -> str:
    return "—" if x is None else f"{x:+.2%}"


def _pct0(x) -> str:
    return "—" if x is None else f"{x:.1%}"


def _num(x) -> str:
    return "—" if x is None else f"{x:.3f}"


def _usd(x) -> str:
    return f"${x:,.2f}"


def summary(ledger: Ledger, since: datetime | None = None) -> dict:
    where, params = ("created_at >= ?", (since.strftime("%Y-%m-%dT%H:%M:%SZ"),)) if since else ("1=1", ())
    rows = ledger.entries(where, params)
    out = {"overall": _agg(rows), "by_league": {}, "by_venue": {}, "by_reference": {}}
    for field, bucket in (("league", "by_league"), ("venue", "by_venue"), ("ref_source", "by_reference")):
        for val in sorted({r[field] for r in rows}):
            out[bucket][val] = _agg([r for r in rows if r[field] == val])
    return out


def format_summary(s: dict) -> str:
    def row(name, a):
        return (f"{name:<16} {a['entries']:>5} {a['open']:>5} {a['settled']:>5} {_pct0(a['hit_rate']):>7} {_usd(a['pnl']):>11} {_pct(a['roi']):>8} "
                f"{_pct(a['avg_edge']):>8} {_pct(a['avg_clv']):>8} ({a['clv_n']}) {_pct(a['avg_venue_clv']):>8} pend_sharp={a['pending_sharp']}")

    head = f"{'group':<16} {'n':>5} {'open':>5} {'setl':>5} {'hit':>7} {'P&L':>11} {'ROI':>8} {'edge':>8} {'CLV':>8}      {'vCLV':>8}"
    lines = ["PAPER LEDGER (simulated, no real money)", head, row("ALL", s["overall"])]
    for title, key in (("by league", "by_league"), ("by venue", "by_venue"), ("by reference", "by_reference")):
        if s[key]:
            lines.append(f"-- {title}")
            lines += [row(k, v) for k, v in s[key].items()]
    lines.append("CLV = Pinnacle de-vigged close / effective entry price - 1 (after fees). vCLV = venue's own closing mid / avg price - 1.")
    return "\n".join(lines)


def weekly(ledger: Ledger, end: datetime | None = None, tz_name: str = "America/Mexico_City", settings=None) -> str:
    end = end or utc_now()
    start = end - timedelta(days=7)
    tz, tzlabel = _tz(tz_name)
    s_iso, e_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")
    new = ledger.entries("created_at >= ? AND created_at < ?", (s_iso, e_iso))
    settled = ledger.entries("settled_at >= ? AND settled_at < ?", (s_iso, e_iso))
    allrows = ledger.entries()
    arbs = ledger.conn.execute("SELECT * FROM arbs WHERE ts >= ? AND ts < ? ORDER BY profit DESC", (s_iso, e_iso)).fetchall()
    runs = ledger.conn.execute("SELECT COUNT(*) n, SUM(errors) err, AVG(seconds) secs, MAX(note) note FROM runs WHERE ts >= ? AND ts < ?", (s_iso, e_iso)).fetchone()

    def table(groups: dict) -> list[str]:
        out = ["| group | entries | settled | hit rate | P&L | ROI | avg edge | avg CLV (n) | beat close | avg venue CLV | CLV pending |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for k, a in groups.items():
            out.append(f"| {k} | {a['entries']} | {a['settled']} | {_pct0(a['hit_rate'])} | {_usd(a['pnl'])} | {_pct(a['roi'])} | {_pct(a['avg_edge'])} | "
                       f"{_pct(a['avg_clv'])} ({a['clv_n']}) | {_pct0(a['clv_beat'])} | {_pct(a['avg_venue_clv'])} | {a['pending_sharp'] + a['pending_close']} |")
        return out

    def by(rows, field):
        return {v: _agg([r for r in rows if r[field] == v]) for v in sorted({r[field] for r in rows})}

    md = [
        f"# CLV paper tracker — weekly report",
        "",
        f"Window: {start.astimezone(tz):%Y-%m-%d %H:%M} → {end.astimezone(tz):%Y-%m-%d %H:%M} ({tzlabel}). **Paper only — no real money, no orders.**",
        "",
        "## This week",
        "",
        f"- New paper entries: **{len(new)}**; settled this week: **{len(settled)}**; settled P&L: **{_usd(sum(r['pnl'] or 0 for r in settled))}**",
        f"- Arbitrage detections (after fees, depth-limited): **{len(arbs)}**" + (f"; best +{_usd(arbs[0]['profit'])} ({arbs[0]['roi']:.2%}) on {arbs[0]['game_key']}" if arbs else ""),
        f"- Engine cycles: {runs['n'] or 0}, errors: {runs['err'] or 0}, avg cycle {runs['secs'] or 0:.1f}s; last note: `{runs['note'] or '—'}`",
        "",
        "### New entries by league",
        "",
        *table({"ALL": _agg(new), **by(new, "league")}),
        "",
        "### New entries by venue",
        "",
        *table(by(new, "venue")),
        "",
        "## All-time",
        "",
        *table({"ALL": _agg(allrows), **by(allrows, "league")}),
        "",
        "## Top 10 entries by CLV (this week)",
        "",
        "| created | league | game | venue | side | outcome | eff. price | fair@entry (src) | close | CLV | status | P&L |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---|---:|",
    ]
    top = sorted(new, key=lambda r: (r["clv_pct"] is None, -(r["clv_pct"] or 0)))[:10]
    for r in top:
        md.append(f"| {_local(r['created_at'], tz)} | {r['league']} | {r['game_label']} | {r['venue']} | {r['side']} | {r['outcome_label']} | {r['effective_price']:.3f} | "
                  f"{r['fair_entry']:.3f} ({r['ref_source']}) | {_num(r['sharp_fair_close'])} | {_pct(r['clv_pct'])} | {r['status']} | "
                  f"{'—' if r['pnl'] is None else _usd(r['pnl'])} |")
    if arbs:
        md += ["", "## Arbitrage detections (top 10)", "", "| time | kind | game | shares | cost | profit | ROI | legs |", "|---|---|---|---:|---:|---:|---:|---|"]
        for a in arbs[:10]:
            legs = "; ".join(f"{l['venue']} {l['side']} {l['outcome']} @{l['avg_price']}" for l in json.loads(a["legs"]))
            md.append(f"| {_local(a['ts'], tz)} | {a['kind']} | {a['game_key']} | {a['shares']:.0f} | {_usd(a['cost'])} | {_usd(a['profit'])} | {a['roi']:.2%} | {legs} |")
    if settings is not None:
        from .graduation import format_graduation, format_status, graduation, status

        md += ["", "## Measured sample and graduation check (proposed thresholds, paper only)", "", "```",
               format_status(status(ledger, settings, end)), "", format_graduation(graduation(ledger, settings, end)), "```"]
    md += ["", "---", "Definitions: CLV = Pinnacle de-vigged closing prob / effective entry price (incl. fees) − 1. "
           "Venue CLV = the venue's own closing mid / avg entry price − 1. 'CLV pending' = no Pinnacle close yet (feed not configured or game not started)."]
    return "\n".join(md) + "\n"


def write_weekly(ledger: Ledger, out_dir: str, end: datetime | None = None, tz_name: str = "America/Mexico_City", settings=None) -> Path:
    end = end or utc_now()
    tz, _ = _tz(tz_name)
    path = Path(out_dir) / f"weekly-{end.astimezone(tz):%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(weekly(ledger, end, tz_name, settings), encoding="utf-8")
    return path
