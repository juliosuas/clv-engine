"""Main loop: fetch -> match -> snapshot -> arbs -> paper entries -> closing -> settle."""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .alerts import Alert, AlertManager
from .arb import cross_venue, multi_outcome
from .config import Settings
from .leagues import League, get_leagues
from .ledger import Ledger
from .matching import match_games
from .models import MatchedGame
from .orderbook import depth
from .sharp.base import SharpSource
from .strategy import consensus_fair, evaluate, sharp_fair
from .util import iso, parse_datetime, utc_now

log = logging.getLogger("clv_engine")


@dataclass
class CycleStats:
    games: int = 0
    venue_games: int = 0
    sharp_games: int = 0
    entries: int = 0
    arbs: int = 0
    errors: int = 0
    matched_cross: int = 0
    per_league: dict = field(default_factory=dict)


def _label(game: MatchedGame) -> str:
    return " vs ".join(game.labels.get(t, t) for t in game.teams)


class Engine:
    def __init__(self, settings: Settings, ledger: Ledger, polymarket, kalshi, sharp: SharpSource, alerts: AlertManager):
        self.s = settings
        self.ledger = ledger
        self.pm = polymarket
        self.kalshi = kalshi
        self.sharp = sharp
        self.alerts = alerts
        self.leagues: list[League] = get_leagues(settings.leagues)
        self._stop = False

    # ---- one league ------------------------------------------------------
    def collect(self, league: League, now: datetime, stats: CycleStats) -> list[MatchedGame]:
        venue_games = []
        for adapter in (self.pm, self.kalshi):
            if adapter is None:
                continue
            try:
                venue_games.extend(adapter.fetch_games(league, self.s.lookahead_hours, now))
            except Exception:  # noqa: BLE001
                stats.errors += 1
                log.exception("%s fetch failed for %s", type(adapter).__name__, league.key)
        sharp_games = []
        try:
            sharp_games = [g for g in self.sharp.fetch(league, now) if g.start and now - timedelta(hours=3) <= g.start <= now + timedelta(hours=self.s.lookahead_hours)]
        except Exception:  # noqa: BLE001
            stats.errors += 1
            log.exception("sharp fetch failed for %s", league.key)
        games = match_games(league.key, league.three_way, venue_games, sharp_games, self.s.match_window_hours)
        stats.venue_games += len(venue_games)
        stats.sharp_games += len(sharp_games)
        stats.games += len(games)
        stats.matched_cross += sum(1 for g in games if len(g.venues) >= 2)
        stats.per_league[league.key] = {
            "polymarket": sum(1 for v in venue_games if v.venue == "polymarket"),
            "kalshi": sum(1 for v in venue_games if v.venue == "kalshi"),
            "sharp": len(sharp_games),
            "matched_cross_venue": sum(1 for g in games if len(g.venues) >= 2),
            "with_sharp": sum(1 for g in games if g.sharp and g.sharp.sharp and g.venues),
        }
        return games

    def process_game(self, game: MatchedGame, now: datetime, stats: CycleStats) -> None:
        game.key = self.ledger.resolve_game_key(game.league, game.teams, game.start, game.key, self.s.match_window_hours)
        start_source = "sharp" if game.sharp else ("polymarket" if "polymarket" in game.venues else "kalshi")
        refs = {v: vg.ref for v, vg in game.venues.items()}
        if game.sharp:
            refs["sharp"] = game.sharp.ref
        self.ledger.upsert_game(game.key, game.league, game.teams, game.labels, game.start, start_source, refs)

        sf = sharp_fair(game)
        # quotes history
        for v, vg in game.venues.items():
            for k, q in vg.outcomes.items():
                self.ledger.add_quote(game.key, v, k, q.best_ask, q.best_bid, depth(q.yes_asks), None, self.s.snapshot_min_seconds, now)
        if sf:
            for k, p in sf.items():
                self.ledger.add_quote(game.key, "pinnacle", k, round(1 / game.sharp.sharp.odds[k], 6), None, None, p, self.s.snapshot_min_seconds, now)
        self.ledger.commit()

        # closing capture: keep overwriting until kickoff; last pre-start value is the close
        if game.start and now < game.start:
            if sf:
                for k, p in sf.items():
                    self.ledger.capture_closing(game.key, k, "pinnacle", p, now)
            cf = consensus_fair(game)
            if cf:
                for k, p in cf.items():
                    self.ledger.capture_closing(game.key, k, "consensus", p, now)
            for v, vg in game.venues.items():
                for k, q in vg.outcomes.items():
                    if q.mid is not None:
                        self.ledger.capture_closing(game.key, k, v, q.mid, now)

        self.scan_arbs(game, stats)
        self.paper_entries(game, now, stats)
        self.soft_book_entries(game, now, stats)

    def scan_arbs(self, game: MatchedGame, stats: CycleStats) -> None:
        arbs = [a for vg in game.venues.values() for a in [multi_outcome(game, vg, self.s.arb_max_shares)] if a]
        arbs += cross_venue(game, self.s.arb_max_shares)
        for a in arbs:
            if a.profit < self.s.arb_min_profit_usd or a.roi < self.s.arb_min_roi:
                continue
            stats.arbs += 1
            self.ledger.add_arb(a.kind, a.league, a.game_key, a.legs, a.shares, a.cost, a.fees, a.profit, a.roi)
            legs_txt = "; ".join(f"{l['venue']} {l['side'].upper()} {game.labels.get(l['outcome'], l['outcome'])} @ {l['avg_price']:.3f} (fee {l['fee']:.2f})" for l in a.legs)
            key = "arb:" + a.game_key + ":" + ",".join(sorted(f"{l['venue']}/{l['outcome']}/{l['side']}" for l in a.legs))
            self.alerts.emit(Alert("arb", key, f"{a.kind} {_label(game)}: +${a.profit:.2f} on ${a.cost:.2f} ({a.roi:.2%}) for {a.shares:.0f} sh",
                                   legs_txt + " | paper only; check settlement rules match", {"game": a.game_key, "legs": a.legs, "profit": a.profit}))

    def paper_entries(self, game: MatchedGame, now: datetime, stats: CycleStats) -> None:
        for v, vg in game.venues.items():
            for c in evaluate(game, vg, self.s, now):
                if self.ledger.count_entries(game.key, v, c.outcome.key, c.side) >= self.s.max_entries_per_outcome:
                    continue
                f = c.fill
                eid = self.ledger.add_entry(
                    venue=v, league=game.league, game_key=game.key, game_label=_label(game), event_ref=vg.ref,
                    market_ref=c.outcome.market_ref, outcome_key=c.outcome.key, outcome_label=c.outcome.label, side=c.side,
                    start_time=iso(game.start), stake=round(f.notional, 6), shares=round(f.shares, 6),
                    best_ask=(c.outcome.yes_asks if c.side == "yes" else c.outcome.no_asks)[0][0],
                    avg_price=round(f.avg_price, 6), fee=round(f.fee, 6), fee_model=vg.fees.describe(),
                    effective_price=round(f.effective_price, 6), partial=0 if f.complete else 1, depth_levels=f.levels,
                    ref_source=c.ref_source, fair_entry=round(c.fair, 6), sharp_fair_entry=None if c.sharp_fair is None else round(c.sharp_fair, 6),
                    edge_entry=round(c.edge, 6), clv_status="pending_close",
                    settle_venue=v, settle_ref=(c.outcome.ref if v == "polymarket" else c.outcome.market_ref),
                )
                stats.entries += 1
                self.alerts.emit(Alert(
                    "entry", f"entry:{eid}",
                    f"PAPER {v} {c.side.upper()} {c.outcome.label} — {_label(game)}",
                    f"{f.shares:.1f} sh @ {f.avg_price:.3f} + fee {f.fee:.2f} → eff {f.effective_price:.3f} vs fair {c.fair:.3f} ({c.ref_source}); edge {c.edge:+.2%}",
                    {"entry_id": eid, "game": game.key},
                ))

    def soft_book_entries(self, game: MatchedGame, now: datetime, stats: CycleStats) -> None:
        if not (self.s.soft_entries and game.sharp and game.sharp.sharp and game.sharp.soft):
            return
        if game.start and game.start - now < timedelta(minutes=self.s.min_minutes_before_start):
            return
        fair = game.sharp.sharp.fair
        for book, line in game.sharp.soft.items():
            for k, odds in line.odds.items():
                if k not in fair:
                    continue
                ev = fair[k] * odds - 1.0
                if ev < self.s.soft_edge or self.ledger.count_entries(game.key, book, k, "yes") >= self.s.max_entries_per_outcome:
                    continue
                price = 1.0 / odds
                settle_v, settle_ref = None, None
                for v, vg in game.venues.items():
                    if k in vg.outcomes:
                        settle_v = v
                        settle_ref = vg.outcomes[k].ref if v == "polymarket" else vg.outcomes[k].market_ref
                        break
                label = game.labels.get(k, "Draw" if k == "draw" else k)
                eid = self.ledger.add_entry(
                    venue=book, league=game.league, game_key=game.key, game_label=_label(game), event_ref=game.sharp.ref, market_ref="h2h",
                    outcome_key=k, outcome_label=label, side="yes", start_time=iso(game.start), stake=self.s.stake_usd,
                    shares=round(self.s.stake_usd / price, 6), best_ask=round(price, 6), avg_price=round(price, 6), fee=0.0,
                    fee_model="soft book decimal odds (no fee)", effective_price=round(price, 6), partial=0, depth_levels=None,
                    ref_source="pinnacle", fair_entry=round(fair[k], 6), sharp_fair_entry=round(fair[k], 6), edge_entry=round(ev, 6),
                    clv_status="pending_close", settle_venue=settle_v, settle_ref=settle_ref,
                )
                stats.entries += 1
                self.alerts.emit(Alert("soft_value", f"soft:{eid}", f"PAPER {book} {label} @ {odds:.2f} — {_label(game)}",
                                       f"Pinnacle fair {fair[k]:.3f} (odds {1/fair[k]:.2f}); EV {ev:+.2%}", {"entry_id": eid}))

    # ---- post-start bookkeeping -----------------------------------------
    def finalize_clv(self, now: datetime) -> int:
        n = 0
        for e in self.ledger.entries("clv_status != 'done'"):
            start = parse_datetime(e["start_time"])
            if start is None or start > now:
                continue
            k, side = e["outcome_key"], e["side"]
            flip = (lambda x: x) if side == "yes" else (lambda x: 1.0 - x)
            close = self.ledger.closing(e["game_key"], k, "pinnacle")
            if close is None and self.sharp.available:
                g = self.ledger.game(e["game_key"])
                ref = json.loads(g["refs"] or "{}").get("sharp") if g else None
                league = next((l for l in self.leagues if l.key == e["league"]), None)
                if ref and league:
                    back = self.sharp.closing(league, ref, start)
                    if back and back.sharp and k in back.sharp.fair:
                        close = back.sharp.fair[k]
                        self.ledger.capture_closing(e["game_key"], k, "pinnacle", close, start)
            fields = {}
            vm = self.ledger.closing(e["game_key"], k, e["venue"]) if e["venue"] in ("polymarket", "kalshi") else None
            if vm is not None:
                vm = flip(vm)
                fields.update(venue_mid_close=round(vm, 6), venue_clv_pct=round(vm / e["avg_price"] - 1, 6))
            if close is not None:
                c = flip(close)
                fields.update(sharp_fair_close=round(c, 6), clv_pct=round(c / e["effective_price"] - 1, 6),
                              clv_pct_raw=round(c / e["avg_price"] - 1, 6), clv_status="done")
            else:
                fields["clv_status"] = "pending_sharp"
            self.ledger.update_entry(e["id"], **fields)
            n += 1
        return n

    def settle(self, now: datetime) -> int:
        n = 0
        for e in self.ledger.entries("status='open'"):
            start = parse_datetime(e["start_time"])
            if start is None or now < start + timedelta(hours=self.s.settle_after_hours):
                continue
            value = None
            try:
                if e["settle_venue"] == "polymarket" and self.pm and e["settle_ref"]:
                    g = self.ledger.game(e["game_key"])
                    slug = json.loads(g["refs"] or "{}").get("polymarket") if g else None
                    value = self.pm.resolution(slug or e["event_ref"], e["settle_ref"]) if (slug or e["venue"] == "polymarket") else None
                elif e["settle_venue"] == "kalshi" and self.kalshi and e["settle_ref"]:
                    value = self.kalshi.resolution(e["settle_ref"])
            except Exception:  # noqa: BLE001
                log.exception("settlement lookup failed for entry %s", e["id"])
            if value is None:
                continue
            held = value if e["side"] == "yes" else 1.0 - value
            pnl = e["shares"] * held - e["stake"] - e["fee"]
            self.ledger.update_entry(e["id"], status="settled", result_value=held, pnl=round(pnl, 6), settled_at=iso(now))
            n += 1
        return n

    # ---- loop -------------------------------------------------------------
    def cycle(self, now: datetime | None = None) -> CycleStats:
        now = now or utc_now()
        t0 = time.monotonic()
        stats = CycleStats()
        for league in self.leagues:
            for game in self.collect(league, now, stats):
                try:
                    self.process_game(game, now, stats)
                except Exception:  # noqa: BLE001
                    stats.errors += 1
                    log.exception("processing %s failed", game.key)
        clv = self.finalize_clv(now)
        settled = self.settle(now)
        secs = time.monotonic() - t0
        note = f"sharp={self.sharp.name}; clv_updates={clv}; settled={settled}"
        self.ledger.add_run(seconds=round(secs, 2), games=stats.games, venue_games=stats.venue_games, sharp_games=stats.sharp_games,
                            entries=stats.entries, arbs=stats.arbs, errors=stats.errors, note=note)
        log.info("cycle %.1fs games=%d venue_games=%d cross=%d sharp=%d entries=%d arbs=%d errors=%d %s",
                 secs, stats.games, stats.venue_games, stats.matched_cross, stats.sharp_games, stats.entries, stats.arbs, stats.errors, note)
        return stats

    def maybe_weekly_report(self, now: datetime | None = None) -> str | None:
        """Write reports/weekly-YYYY-MM-DD.md once on the configured weekday/hour (report TZ)."""
        from pathlib import Path

        from .report import _tz, write_weekly

        if self.s.weekly_report_weekday < 0:
            return None
        tz, _ = _tz(self.s.report_tz)
        local = (now or utc_now()).astimezone(tz)
        if local.weekday() != self.s.weekly_report_weekday or local.hour < self.s.weekly_report_hour:
            return None
        target = Path(self.s.report_dir) / f"weekly-{local:%Y-%m-%d}.md"
        if target.exists():
            return None
        path = write_weekly(self.ledger, self.s.report_dir, now, self.s.report_tz)
        self.alerts.emit(Alert("system", f"weekly:{local:%Y-%m-%d}", f"Weekly CLV paper report written: {path}"))
        return str(path)

    def run_forever(self) -> None:
        def _stop(*_):
            self._stop = True
            log.info("stop requested")

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        if not self.sharp.available:
            log.warning("No sharp feed (%s): entries use cross-venue consensus when ENTRY_REFERENCE=auto; Pinnacle CLV stays pending.",
                        getattr(self.sharp, "reason", self.sharp.name))
        while not self._stop:
            try:
                self.cycle()
            except Exception:  # noqa: BLE001
                log.exception("cycle failed")
            try:
                self.maybe_weekly_report()
            except Exception:  # noqa: BLE001
                log.exception("weekly report failed")
            for _ in range(int(self.s.poll_seconds)):
                if self._stop:
                    break
                time.sleep(1)
