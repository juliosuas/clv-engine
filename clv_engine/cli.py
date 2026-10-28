"""Command line: python -m clv_engine <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from .alerts import AlertManager, StdoutSink, build_sinks, make_alerts, send_test_alert
from .arb import cross_venue, multi_outcome
from .config import Settings
from .engine import Engine
from .http import Http
from .leagues import LEAGUES, get_leagues
from .ledger import Ledger
from .matching import match_games
from .graduation import format_graduation, format_status, graduation, status, to_json
from .report import format_summary, summary, write_weekly
from .sharp.quota import format_plan
from .sharp import make_sharp_source
from .strategy import consensus_fair, sharp_fair
from .util import utc_now
from .venues.kalshi import KalshiAdapter
from .venues.polymarket import PolymarketAdapter


def build(settings: Settings, ledger: Ledger | None = None, alerts: AlertManager | None = None) -> Engine:
    http = Http()
    ledger = ledger or Ledger(settings.db_path)
    return Engine(
        settings, ledger,
        PolymarketAdapter(http, fetch_no_books=settings.fetch_no_books),
        KalshiAdapter(http, fee_rounding=settings.kalshi_fee_rounding),
        make_sharp_source(settings, http, ledger),
        alerts or make_alerts(settings, ledger),
    )


def cmd_scan(settings: Settings, args) -> int:
    """Read-only live scan: prints matched games, best asks, fair refs and arbs. Writes nothing."""
    http = Http()
    pm = PolymarketAdapter(http, fetch_no_books=settings.fetch_no_books)
    k = KalshiAdapter(http, fee_rounding=settings.kalshi_fee_rounding)
    sharp = make_sharp_source(settings, http)
    now = utc_now()
    keys = [args.league] if args.league else settings.leagues
    report = {}
    for league in get_leagues(keys):
        vgs = pm.fetch_games(league, args.hours, now) + k.fetch_games(league, args.hours, now)
        sgs = sharp.fetch(league, now)
        games = match_games(league.key, league.three_way, vgs, sgs, settings.match_window_hours)
        rows = []
        for g in sorted(games, key=lambda g: (g.start or now)):
            arbs = [a for vg in g.venues.values() for a in [multi_outcome(g, vg, settings.arb_max_shares)] if a] + cross_venue(g, settings.arb_max_shares)
            sf, cf = sharp_fair(g), consensus_fair(g)
            rows.append({
                "game": " vs ".join(g.labels.get(t, t) for t in g.teams),
                "start_utc": g.start.strftime("%Y-%m-%dT%H:%MZ") if g.start else None,
                "venues": {v: {o.label: {"ask": o.best_ask, "bid": o.best_bid, "ask_depth_sh": round(sum(s for _, s in o.yes_asks), 1)} for o in vg.outcomes.values()} for v, vg in g.venues.items()},
                "sum_yes_asks": {v: round(sum(o.best_ask or 0 for o in vg.outcomes.values()), 4) for v, vg in g.venues.items() if vg.exhaustive},
                "pinnacle_fair": {g.labels.get(kk, kk): round(p, 4) for kk, p in sf.items()} if sf else "pending (no ODDS_API_KEY)",
                "consensus_fair": {g.labels.get(kk, kk): round(p, 4) for kk, p in cf.items()} if cf else None,
                "arbs": [{"kind": a.kind, "shares": a.shares, "profit": round(a.profit, 2), "roi": round(a.roi, 4), "legs": a.legs} for a in arbs],
            })
        report[league.key] = {
            "polymarket_games": sum(1 for v in vgs if v.venue == "polymarket"),
            "kalshi_games": sum(1 for v in vgs if v.venue == "kalshi"),
            "matched_cross_venue": sum(1 for g in games if len(g.venues) >= 2),
            "sharp_games": len(sgs),
            "games": rows[: args.limit],
        }
    print(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    print(f"# http GET calls: {http.calls}; sharp source: {sharp.name}", file=sys.stderr)
    return 0


def cmd_test_alert(settings: Settings) -> int:
    """One test message through every configured sink. Exit 1 if any fails, 2 on bad config."""
    import socket
    try:
        sinks = build_sinks(settings, strict=True)
    except ValueError as exc:
        print(f"alert config error: {exc}", file=sys.stderr)
        return 2
    text = (f"PAPER clv-engine test alert from {socket.gethostname()} at {utc_now():%Y-%m-%d %H:%M} UTC. "
            "Delivery check only: no entries, no orders, no real money.")
    results = send_test_alert(sinks, text)
    for name, err in results.items():
        print(f"{name}: {'ok' if err is None else 'FAILED (' + err + ')'}")
    if not results:
        print("note: no sinks configured (ALERT_SINKS is empty and no webhook/Telegram secrets set)")
    elif not any(n in results for n in ("webhook", "telegram")):
        print("note: no remote sink configured (set ALERT_WEBHOOK_URL or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")
    return 1 if any(err is not None for err in results.values()) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clv_engine", description="PAPER-ONLY odds/CLV tracker (read-only market data).")
    ap.add_argument("--env-file", default=".env")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the loop (24/7) or a single cycle with --once")
    r.add_argument("--once", action="store_true")
    s = sub.add_parser("scan", help="live read-only scan, prints JSON, writes nothing")
    s.add_argument("--league", choices=sorted(LEAGUES))
    s.add_argument("--hours", type=float, default=72)
    s.add_argument("--limit", type=int, default=10)
    rep = sub.add_parser("report", help="ledger reports")
    rep.add_argument("kind", choices=["summary", "weekly", "entries"])
    rep.add_argument("--days", type=float, default=None, help="summary: only entries created in the last N days")
    rep.add_argument("--out", default=None, help="weekly: output dir (default REPORT_DIR)")
    st = sub.add_parser("status", help="measured-sample status: Pinnacle closes, mean CLV + CI, entries/day, days to target")
    st.add_argument("--json", action="store_true")
    gr = sub.add_parser("graduation", help="paper->real graduation check against the proposed numeric gate (reporting only)")
    gr.add_argument("--json", action="store_true")
    sub.add_parser("quota", help="Odds API credit plan estimate and today's local usage (no network)")
    sub.add_parser("settle", help="finalize CLV and settle finished games")
    sub.add_parser("init-db", help="create the SQLite schema")
    sub.add_parser("test-alert", help="send one test message through every configured sink; exit 1 if one fails")
    args = ap.parse_args(argv)

    settings = Settings.from_env(args.env_file)
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "scan":
        return cmd_scan(settings, args)
    if args.cmd == "test-alert":
        return cmd_test_alert(settings)
    ledger = Ledger(settings.db_path)
    if args.cmd == "init-db":
        print(f"ledger ready at {settings.db_path}")
        return 0
    if args.cmd == "status":
        res = status(ledger, settings)
        print(to_json(res) if args.json else format_status(res))
        return 0
    if args.cmd == "graduation":
        res = graduation(ledger, settings)
        print(to_json(res) if args.json else format_graduation(res))
        return 0
    if args.cmd == "quota":
        active = [lg for lg in get_leagues(settings.leagues) if lg.key not in settings.excluded_leagues]
        print(format_plan(settings, len(active)))
        u = ledger.usage(utc_now().strftime("%Y-%m-%d"))
        print(f"today (UTC, local count): {u['credits']:.0f}/{settings.odds_api_daily_budget} credits, {u['requests']} calls, {u['skipped']} skipped, "
              f"last x-requests-remaining={u['remaining']}")
        return 0
    if args.cmd == "report":
        if args.kind == "summary":
            since = utc_now() - timedelta(days=args.days) if args.days else None
            print(format_summary(summary(ledger, since)))
        elif args.kind == "weekly":
            path = write_weekly(ledger, args.out or settings.report_dir, tz_name=settings.report_tz, settings=settings)
            print(path)
        else:
            for e in ledger.entries():
                print(json.dumps(dict(e), default=str))
        return 0
    engine = build(settings, ledger)
    if args.cmd == "settle":
        now = utc_now()
        print(f"clv updated: {engine.finalize_clv(now)}; settled: {engine.settle(now)}")
        return 0
    if args.cmd == "run":
        if args.once:
            st = engine.cycle()
            print(json.dumps(st.__dict__, indent=1, default=str))
        else:
            engine.run_forever()
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
