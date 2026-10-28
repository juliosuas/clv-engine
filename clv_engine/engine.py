"""Main loop: fetch -> match -> snapshot -> arbs -> paper entries -> closing -> settle."""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass, field, replace
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
from .strategy import consensus_fair, entry_allowed, evaluate, sharp_fair
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
        self._sharp_ts: dict[str, datetime | None] = {}
        self._missing_logged: set[int] = set()
        self._hist_tried: set[int] = set()
        self._stale_logged: set[tuple[str, str]] = set()

    def league_active(self, league: League, now: datetime) -> bool:
        """Excluded leagues are only polled while an upcoming game still has entries waiting for a close."""
        if league.key not in self.s.excluded_leagues:
            return True
        return any(k >= now for k in self.ledger.pending_kickoffs(league.key))

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
            self.sharp.set_pending(league.key, [k for k in self.ledger.pending_kickoffs(league.key) if k >= now])
            fetched = [g for g in self.sharp.fetch(league, now) if g.start and now - timedelta(hours=3) <= g.start <= now + timedelta(hours=self.s.lookahead_hours)]
            self._sharp_ts[league.key] = self.sharp.fetched_at(league.key)
            sharp_games = self._drop_stale_lines(league, fetched, now)
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

    def _drop_stale_lines(self, league: League, games: list, now: datetime) -> list:
        """Pinnacle/soft prices older than MAX_SHARP_AGE_MINUTES (budget exhausted, outage) are not a fair reference:
        keep the events for matching but strip the lines so no entry or closing capture uses them."""
        if not games or not self.sharp.available:
            return games
        ts = self._sharp_ts.get(league.key)
        limit = timedelta(minutes=self.s.max_sharp_age_minutes)
        if ts is not None and now - ts <= limit:
            return games
        k = (league.key, now.strftime("%Y-%m-%d"))
        if k not in self._stale_logged:
            self._stale_logged.add(k)
            age = "never" if ts is None else f"{(now - ts).total_seconds() / 60:.0f} min ago"
            log.warning("Pinnacle data for %s is stale (last sample %s > MAX_SHARP_AGE_MINUTES=%g): no new entries or closes from it until a fresh poll",
                        league.key, age, self.s.max_sharp_age_minutes)
        return [replace(g, sharp=None, soft={}) for g in games]

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
                sampled = self._sharp_ts.get(game.league) or now  # when Pinnacle was actually polled, not this cycle
                for k, p in sf.items():
                    self.ledger.capture_closing(game.key, k, "pinnacle", p, min(sampled, now))
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
        if game.league in self.s.excluded_leagues:
            return
        if game.start and game.start - now < timedelta(minutes=self.s.min_minutes_before_start):
            return
        fair = game.sharp.sharp.fair
        for book, line in game.sharp.soft.items():
            if not entry_allowed(game.league, book, self.s):
                continue
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
    def _pinnacle_close(self, e, start: datetime, now: datetime) -> tuple[float | None, str]:
        """(close fair prob of the YES side of the outcome, reason if none). A close only counts when the last
        Pinnacle sample is within CLOSE_MAX_AGE_MINUTES of kickoff."""
        k = e["outcome_key"]
        max_age = timedelta(minutes=self.s.close_max_age_minutes)
        reason = ""
        row = self.ledger.closing_row(e["game_key"], k, "pinnacle")
        if row is not None:
            value, at = row
            if max_age.total_seconds() <= 0 or (at is not None and at >= start - max_age):
                return value, ""
            gap = "unknown time" if at is None else f"{(start - at).total_seconds() / 60:.0f} min before kickoff"
            reason = f"last Pinnacle sample was {gap} (CLOSE_MAX_AGE_MINUTES={self.s.close_max_age_minutes:g})"
        if self.sharp.available and e["id"] not in self._hist_tried:
            g = self.ledger.game(e["game_key"])
            ref = json.loads(g["refs"] or "{}").get("sharp") if g else None
            league = next((l for l in self.leagues if l.key == e["league"]), None)
            if ref and league and self.s.odds_api_historical:
                back = self.sharp.closing(league, ref, start, now)
                if not self.sharp.closing_blocked:
                    self._hist_tried.add(e["id"])
                if back and back.sharp and k in back.sharp.fair:
                    value = back.sharp.fair[k]
                    self.ledger.capture_closing(e["game_key"], k, "pinnacle", value, start - timedelta(minutes=1))
                    return value, ""
                reason = (reason + "; " if reason else "") + ("historical backfill found nothing" if not self.sharp.closing_blocked else "historical backfill blocked by credit budget")
        if reason:
            return None, reason
        if not self.sharp.available:
            return None, f"no Pinnacle feed ({getattr(self.sharp, 'reason', self.sharp.name)})"
        league = next((l for l in self.leagues if l.key == e["league"]), None)
        if league is None:
            return None, f"league {e['league']} is not in LEAGUES (not polled)"
        if not league.odds_api_sport:
            return None, f"league {league.key} has no Odds API sport key"
        skipped = getattr(getattr(self.sharp, "quota", None), "usage", None)
        skipped_n = skipped(now)["skipped"] if skipped else 0
        if skipped_n:
            return None, f"no Pinnacle sample for this game ({skipped_n} Odds API polls skipped today by the credit budget; ODDS_API_HISTORICAL=0)"
        return None, "no Pinnacle line was ever sampled for this game (event not matched in the Odds API feed, or entry opened after the last poll)"

    def finalize_clv(self, now: datetime) -> int:
        n = 0
        for e in self.ledger.entries("clv_status != 'done'"):
            start = parse_datetime(e["start_time"])
            if start is None or start > now:
                continue
            k, side = e["outcome_key"], e["side"]
            flip = (lambda x: x) if side == "yes" else (lambda x: 1.0 - x)
            close, why = self._pinnacle_close(e, start, now)
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
                if e["id"] not in self._missing_logged:
                    self._missing_logged.add(e["id"])
                    log.warning("Pinnacle close NOT captured: entry #%s %s %s %s %s | %s | kickoff %s | %s",
                                e["id"], e["league"], e["venue"], e["side"].upper(), e["outcome_label"], e["game_label"], e["start_time"], why)
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
            if not self.league_active(league, now):
                continue
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
        quota = getattr(self.sharp, "quota", None)
        if quota is not None:
            u = quota.usage(now)
            note += (f"; credits_today={u['credits']:.0f}/{self.s.odds_api_daily_budget} calls={u['requests']} skipped={u['skipped']}"
                     f" remaining={quota.last_remaining if quota.last_remaining is not None else 'n/a'}")
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
        path = write_weekly(self.ledger, self.s.report_dir, now, self.s.report_tz, self.s)
        self.alerts.emit(Alert("system", f"weekly:{local:%Y-%m-%d}", f"Weekly CLV paper report written: {path}"))
        return str(path)

    def run_forever(self) -> None:
        def _stop(*_):
            self._stop = True
            log.info("stop requested")

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        if not self.sharp.available:
            log.warning("No sharp feed (%s): Pinnacle CLV stays pending%s.", getattr(self.sharp, "reason", self.sharp.name),
                        "; REQUIRE_PINNACLE_REF=1 so no new paper entries are opened (consensus entries are off)" if self.s.require_pinnacle_ref
                        else "; entries use cross-venue consensus when ENTRY_REFERENCE=auto")
        elif hasattr(self.sharp, "quota"):
            from .sharp.quota import format_plan

            active = [lg for lg in self.leagues if lg.key not in self.s.excluded_leagues]
            log.info(format_plan(self.s, len(active)))
        log.info("Entry filters: MIN_EDGE=%.1f%% after fees vs Pinnacle (REQUIRE_PINNACLE_REF=%s), MIN_DEPTH_USD=%g, full fill of STAKE_USD=%g required; "
                 "excluded leagues=%s venues=%s", self.s.min_edge * 100, int(self.s.require_pinnacle_ref), self.s.min_depth_usd,
                 self.s.stake_usd, ",".join(self.s.excluded_leagues) or "-", ",".join(self.s.excluded_venues) or "-")
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
