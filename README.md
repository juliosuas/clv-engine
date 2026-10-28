# clv-engine — paper-only odds / CLV tracker

Tracks **Polymarket** and **Kalshi** sports moneylines (NBA, NFL, Premier League, La Liga, Liga MX; Champions League
excluded for now) against **Pinnacle's de-vigged price and closing line**. It writes simulated
("paper") entries to SQLite, scans for fee-aware arbitrage, sends alerts and writes weekly reports.

> **PAPER ONLY.** Zero real money. No wallet, no private keys, no exchange API keys, no order or
> trading endpoints. Every venue call is a public, unauthenticated `GET`
> (see `clv_engine/http.py`; `tests/test_sharp_report_safety.py` guards this statically). The only
> outbound `POST`s are alerts: Telegram `sendMessage` and/or a webhook *you* configure. Secrets come from env vars only.
>
> **Not financial advice.** This is a research tool that simulates bets on paper. Nothing here is a
> recommendation to bet or trade, and past paper results say nothing about real results.

## What it does each cycle (default every 120 s)

1. **Discover** upcoming games (next `LOOKAHEAD_HOURS`) per league:
   - Polymarket Gamma `GET /events?series_id=…` (series ids from `GET /sports`), moneyline markets only.
     NBA = one two-outcome market; soccer = neg-risk group of 3 Yes/No markets (home / draw / away).
   - Kalshi `GET /trade-api/v2/events?series_ticker=KX…GAME&with_nested_markets=true`.
2. **Price** from real order books: Polymarket CLOB `GET /book?token_id=` (YES and NO tokens);
   Kalshi `GET /markets/orderbooks?tickers=…` (bids only, so a NO bid at p is a YES ask at 1−p).
3. **Sharp reference** (optional, `SHARP_SOURCE=theoddsapi` + `ODDS_API_KEY`): Pinnacle `h2h` from
   The Odds API, de-vigged (`DEVIG_METHOD`), plus soft books (`SOFT_BOOKS`).
4. **Match** games across venues: same league, same team pair (alias table + normalization), kickoff
   within `MATCH_WINDOW_HOURS`.
5. **Record** quote history (deduped), and keep overwriting the **closing line** per outcome
   (Pinnacle fair, cross-venue consensus, each venue's mid) until kickoff. The last pre-kickoff value is the close.
6. **Arbitrage scan** (detection only): multi-outcome (sum of YES asks over 1X2 < $1) and cross-venue
   (mixed YES legs, or YES on X at one venue + NO on X at the other), **after taker fees and limited by depth**.
7. **Paper entries** when the venue's all-in price beats the fair reference by `MIN_EDGE`
   (see entry rule below), filled against real book depth for `STAKE_USD`.
8. After kickoff: compute **CLV**. After `SETTLE_AFTER_HOURS`: **settle** from the venue's own resolution and book P&L.

## Fees (verified 2026-10-18)

| Venue | Formula | Source |
|---|---|---|
| Polymarket | `fee = C × feeRate × p × (1−p)`, takers only, 5-dp USDC. Sports `feeRate = 0.05`; the engine reads each market's `feeSchedule.rate` | https://docs.polymarket.com/trading/fees |
| Kalshi | `fee = round_up(0.07 × C × P × (1−P))` to the cent × series `fee_multiplier` (read from `GET /series/{ticker}`; currently 1 for the game series) | General Trading Fees Table in https://kalshi.com/docs/kalshi-fee-schedule.pdf (linked from the `fee_type` docs at https://docs.kalshi.com/api-reference/market/get-series); rounding mechanics: https://docs.kalshi.com/getting_started/fee_rounding |

Multi-level fills: Polymarket fee is summed per level; Kalshi's is computed over the whole order and
rounded up once. `KALSHI_FEE_ROUNDING=micro` models direct-member $0.0001 precision. The Kalshi fee PDF
itself sits behind a bot checkpoint from servers; the API docs' `fee_type` description was used to confirm
that `quadratic` / `quadratic_with_maker_fees` share the general taker table.

## De-vig

`clv_engine/devig.py`, all methods return fair probabilities that sum to 1:

- **multiplicative**: `p_i = (1/o_i) / Σ(1/o_j)`. Simple; leaves longshots a bit rich.
- **power** (default): find `k` with `Σ (1/o_i)^k = 1`. Takes more margin off longshots.
- **shin**: Shin (1993) insider model; solve for `z` so `Σ p_i(z) = 1`. Also shades longshots.

## Entry rule and CLV definitions

- `effective_price = (Σ level price × shares + taker fee) / shares` for a `STAKE_USD` fill walked down the real book.
- `edge = fair / effective_price − 1`. Enter if `edge ≥ MIN_EDGE`, the fill is complete (unless `ALLOW_PARTIAL=1`),
  best ask is within `[MIN_PRICE, MAX_PRICE]`, kickoff is ≥ `MIN_MINUTES_BEFORE_START` away, and there are fewer than
  `MAX_ENTRIES_PER_OUTCOME` entries for that venue/outcome/side. Sides: buying YES or buying NO on each outcome (`ENTRY_SIDES`).
- `fair` comes from `ENTRY_REFERENCE`:
  - `sharp`: Pinnacle de-vigged.
  - `consensus`: the *other* venue's normalized mids.
  - `auto` (default): Pinnacle when available, else consensus.
  Each entry stores `ref_source`, plus `sharp_fair_entry` when Pinnacle was available.
- **CLV** (`clv_pct`) = Pinnacle de-vigged close / effective_price − 1, so it is net of fees. `clv_pct_raw` uses the price before fees.
  `venue_clv_pct` = the venue's own closing mid / avg price − 1, which you get even without a sharp feed.
- `clv_status`:
  - `pending_close`: the game hasn't started yet.
  - `pending_sharp`: the game started, but there is no Pinnacle close (no key, or the feed was missing).
  - `done`: CLV computed.
- Soft books: when a soft book's decimal odds × Pinnacle fair − 1 ≥ `SOFT_EDGE`, a paper entry is logged with
  `venue=<book key>`, no fee and no depth limit. It is settled through the matched Polymarket/Kalshi outcome when one exists.

## Running without The Odds API (today)

Leave `ODDS_API_KEY` empty (or `SHARP_SOURCE=none`). Everything else still runs: discovery, books, matching, quote history,
venue closing mids, arbitrage, alerts, settlement, reports. Pinnacle CLV stays
`pending_sharp`. If you later set `ODDS_API_HISTORICAL=1` (paid plan), missing closes are backfilled from
`/v4/historical/sports/{sport}/events/{id}/odds`, but only for games where the engine stored the Odds API event id,
so for games before the key existed the Pinnacle CLV stays pending.

Note: The Odds API has **no Caliente** and only **Codere Italy (`codere_it`)**, not Codere MX (bookmaker list checked
2026-10-18). The soft-book adapter is generic (`SOFT_BOOKS=` any Odds API keys). Getting Caliente would need another source.

## Pinnacle close capture and the Odds API credit budget

Free plan = **500 credits per calendar month**; `ODDS_API_MONTHLY_BUDGET=500` is the plan, `ODDS_API_HARD_CAP=450` is the
local ceiling (50 credits of margin). One `/odds` call per sport returns every event, so polls are grouped **per sport and kickoff
round**: kickoffs of a sport within `CLOSE_WINDOW_MAX_MINUTES - CLOSE_WINDOW_MIN_MINUTES` (4 min) share one round. Requests are
`markets=h2h`, `regions=eu`, `bookmakers=pinnacle` only (`bookmakers` overrides `regions`; 1 market × 1 region = 1 credit;
empty responses, `/sports` and `/events` are free). With `SOFT_ENTRIES=1` the `SOFT_BOOKS` ride along (still 1 credit for ≤10 books).

| poll | when | credits |
|---|---|---|
| final close | the last engine cycle inside T-5..T-1 min of a round that has pending entries | 1 per sport and round |
| early close | start of the window, only with `CLOSE_POLLS_PER_ROUND=2` | 1 |
| entry | `ENTRY_POLL_LEAD_MINUTES` (25) before the round's first kickoff (fresh lines until T-5 with `MAX_SHARP_AGE_MINUTES=20`) | 1 |

- Kickoff times come for free from the venue games (Polymarket/Kalshi), so no discovery poll is spent. Nothing is polled outside
  these slots and there is no historical call unless `ODDS_API_HISTORICAL=1`.
- **Priority:** final close > early close > entry. Final closes are stopped only by the hard cap or the provider's `x-requests-remaining`
  reaching 0; optional polls follow a daily allowance (`(spendable + spent today) / days left in the month`, at most
  `ODDS_API_DAILY_BURST` × the even daily share) and must leave the credits already owed to final closes of pending rounds.
  Entry polls may only use `1 - ODDS_API_CLOSE_RESERVE` of the allowance and stop below `ODDS_API_MIN_REMAINING`.
- **Calendar month, headers are the truth:** the local ledger counts per UTC calendar month (`api_usage`); every response's
  `x-requests-used/remaining/last` replaces the local estimate. Headers from an earlier month are ignored after the month changes
  (unverified: the provider's exact reset instant; check the dashboard).
- **Degradation:** if a final close poll cannot be paid, one `WARNING` names the sport and round, a `missed_closes` row is stored and
  the entries of that round become `no_close` (excluded from CLV stats, never invented). `python3 -m clv_engine quota` prints the
  plan estimate (credits/day and /month for the configured leagues, no network), this month's usage and lost close rounds.
- A Pinnacle close only counts if fetched within `CLOSE_MAX_AGE_MINUTES` of kickoff (`closing.captured_at` = fetch time). Every entry
  whose close was not captured gets one `WARNING` (`Pinnacle close NOT captured: entry #… | reason`).
- Removed in this version (ignored, a WARNING names any still set): `ODDS_API_DAILY_BUDGET`, `SHARP_*POLL_SECONDS`,
  `NEAR_WINDOW_MINUTES`, `IDLE_AFTER_MINUTES`, `CLOSE_CAPTURE_MINUTES`, `CLOSE_DENSE_PENDING_ONLY`, `PLAN_KICKOFF_CLUSTERS`.
- Historical backfill (`ODDS_API_HISTORICAL=1`) costs 10× per call and uses the same budget guard; off by default (free plan).

## Entry filters (defaults, all env-configurable)

- `MIN_EDGE=0.05`: at least 5% edge vs Pinnacle no-vig fair, **after fees** (the edge uses `effective_price`).
- `REQUIRE_PINNACLE_REF=1`: entries need a fresh Pinnacle reference (no consensus fallback). Consequence: **with no
  `ODDS_API_KEY` no new entries are opened**; set it to 0 to bring back the `ENTRY_REFERENCE=auto` fallback.
- `MIN_DEPTH_USD=250` of ask depth within `DEPTH_TOLERANCE=0.01` of the best ask, and the full `STAKE_USD` fill is required.
- `EXCLUDED_LEAGUES=ucl` and `EXCLUDED_VENUES=kalshi`: temporarily excluded from entries (Kalshi is still fetched for venue CLV
  and arbs; an excluded league is only polled while it has a pending entry awaiting its close). Existing rows are never changed.
- League priority: `LEAGUES=nba,nfl,epl,laliga,ligamx,ucl`. NFL is supported by both venue adapters (Polymarket series 12185,
  Kalshi `KXNFLGAME`, recorded fixtures 2026-10-27). The Pinnacle NFL feed (`americanfootball_nfl`) has only been exercised with
  a synthetic fixture because no key is available.

## Edge vs venue-CLV audit

`python3 -m clv_engine audit [--dump rows.json] [--quotes quotes.json]` decomposes why the declared entry edge (reference / all-in price - 1, fee
included) can be positive while `venue_clv_pct` (own venue's close mid / avg price - 1, no fee) is negative: the gap is exactly
(reference -> close price move) x (fee factor). `venue_clv_pct` is own-venue drift, not CLV. Fixes on this branch: a mid needs
both sides of the book (`OutcomeQuote.mid2`) for references and closes; equivalent bets in a two-way market (YES A == NO B)
count as one entry; opposite bets on a game are not opened (`ALLOW_HEDGED_ENTRIES=0`). `no_close` is a `clv_status` for entries
whose Pinnacle close could not be captured; they are excluded from the measured sample. Graduation counts distinct games
(`GRAD_MIN_GAMES`, `GRAD_MIN_GROUP_GAMES`, `GRAD_DEADLINE`); paper fills are always taker.
With `--quotes` (or from the ledger's `quotes` history) the audit also splits the spread: reference vs own-book mid and mid vs ask, using the quote
tick nearest to the entry (**approximation**: the book at the entry's own instant is not stored; NO side = 1 − the YES book). See `docs/GRADUATION.md`.

## Status and graduation

```bash
python3 -m clv_engine status [--json]       # entries with Pinnacle close, settled, mean CLV, 90% CI, entries/day, days to 300
python3 -m clv_engine graduation [--json]   # proposed paper->real gate; reporting only
```

`status` reports "insufficient data" instead of a projection until there are ≥10 measured entries over ≥2 days. The gate and the
proposed real-phase risk limits are in [`docs/GRADUATION.md`](docs/GRADUATION.md): proposals for Julio to approve, not facts, and
nothing in this repo places real orders.

## Quick start

```bash
cp .env.example .env          # edit thresholds; leave ODDS_API_KEY empty for now
python3 -m clv_engine scan --league ligamx      # live read-only scan, prints JSON, writes nothing
python3 -m clv_engine run --once                # one full cycle into ./data/clv.sqlite
python3 -m clv_engine run                       # loop forever (Ctrl-C / SIGTERM to stop)
python3 -m clv_engine report summary            # CLI summary (per league / venue / reference)
python3 -m clv_engine report summary --days 7
python3 -m clv_engine report weekly             # writes reports/weekly-YYYY-MM-DD.md
python3 -m clv_engine report entries            # raw entries as JSON lines
python3 -m clv_engine settle                    # finalize CLV + settle finished games now
python3 -m clv_engine status                    # measured sample, mean CLV + CI, projection to 300
python3 -m clv_engine graduation                # proposed paper->real gate (reporting only)
python3 -m clv_engine quota                     # Odds API credit plan + this month's usage
```

Python ≥ 3.11, standard library only (`tzdata` optional, for report time zones on minimal images).
The loop also writes the weekly report on its own (`WEEKLY_REPORT_WEEKDAY`/`WEEKLY_REPORT_HOUR`, default Monday 09:00 America/Mexico_City).

### Reading the ledger directly

```bash
sqlite3 data/clv.sqlite "SELECT created_at, league, game_label, venue, side, outcome_label, round(effective_price,3) px,
  round(fair_entry,3) fair, ref_source, round(edge_entry*100,2) edge_pct, clv_status, round(clv_pct*100,2) clv_pct, status, round(pnl,2) pnl
  FROM entries ORDER BY id DESC LIMIT 20;"
```

Tables:
- `entries`: paper ledger.
- `closing`: last pre-kickoff value per game/outcome/source.
- `quotes`: deduped price history.
- `arbs`: detections.
- `games`: matched games and venue refs.
- `alerts`: dedupe state.
- `runs`: per-cycle health.
- `api_usage`: Odds API requests/credits/skipped polls per UTC day.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest -q
```

Tests run on recorded fixtures (`tests/fixtures/`, captured from the live public APIs on 2026-10-18 and 2026-10-27 (NFL) and trimmed).
`oddsapi_odds_synthetic.json` is hand-written in the documented v4 shape, because no key exists yet. A fixture blocks all sockets,
so tests can never hit the network.

## Deploy on Magno (24/7)

**Docker compose** (recommended):

```bash
cp .env.example .env && chmod 600 .env     # fill in only what's decided (alerts, thresholds)
docker compose up -d --build
docker compose logs -f
docker compose run --rm clv-engine report summary
docker compose run --rm clv-engine report weekly     # -> ./data/reports/
```

The container is non-root, read-only rootfs, drops all capabilities and keeps data in `./data` (SQLite + alerts + reports).

**systemd** (no Docker): copy the repo to `/opt/clv-engine`, `useradd --system clv`, put env in
`/etc/clv-engine/clv-engine.env` (chmod 600), then `cp deploy/clv-engine.service /etc/systemd/system/ && systemctl enable --now clv-engine`.
Data lives in `/var/lib/clv-engine`.

## Alerts

Sinks: stdout and JSONL file (`ALERT_SINKS=stdout,file`), a webhook (`ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_KIND`
slack | discord | generic) and Telegram. Webhook and Telegram turn on by themselves when their secrets are set.
Alerts are deduped per key for `ALERT_COOLDOWN_SECONDS`. A failing sink is logged and never stops the loop.

**Telegram.** Set both `TELEGRAM_BOT_TOKEN` (from @BotFather) and `TELEGRAM_CHAT_ID`; with only one set the
sink stays off. It sends `POST https://api.telegram.org/bot<token>/sendMessage` with `chat_id` and plain `text`
(no `parse_mode`, so nothing needs escaping), 10 s timeout, one retry on timeouts, connection errors, 429 and 5xx.
The token is never logged: errors carry only the HTTP status and Telegram's description, with `/bot<token>` redacted.

**Test delivery.** `python -m clv_engine test-alert` sends one test message through every configured sink, prints
`ok` or `FAILED (reason)` per sink, and exits 1 if any configured sink fails. On Magno (systemd), with the same
root-only env file the service reads:

```bash
sudo systemd-run --pipe --wait --collect -p User=clv -p WorkingDirectory=/opt/clv-engine \
  -p EnvironmentFile=/etc/clv-engine/clv-engine.env \
  -p Environment=ALERT_FILE=/var/lib/clv-engine/alerts.jsonl \
  /usr/bin/python3 -m clv_engine --env-file /dev/null test-alert
sudo systemctl restart clv-engine   # pick up new Telegram env vars
```

With Docker: `docker compose run --rm clv-engine test-alert`.

Keep comments on their own lines in any env file. systemd `EnvironmentFile` (and docker `env_file`) do not strip
inline comments, so `POLL_SECONDS=120  # note` would set the value to `120  # note`. `.env.example` follows this
rule and `tests/test_env_example.py` enforces it.

## Layout

```
clv_engine/
  venues/polymarket.py   Gamma discovery + CLOB books (GET only)
  venues/kalshi.py       public events + orderbooks + series fee multiplier (GET only)
  sharp/                 SharpSource interface; none | theoddsapi (Pinnacle + soft books)
  devig.py fees.py orderbook.py teams.py (+ data/team_aliases.json) matching.py
  sharp/quota.py         kickoff rounds, monthly budget tracker, plan estimate
  graduation.py          status + graduation gate (reporting only)
  strategy.py arb.py ledger.py alerts.py engine.py report.py cli.py config.py
tests/                   pytest, recorded fixtures, no network
deploy/                  systemd unit
```

## Credits

Parts adapted from [juliosuas/polymarket-desk](https://github.com/juliosuas/polymarket-desk) (MIT):
- `get_json` / `build_url` → `http.py`
- `normalize_market` → `venues/polymarket.py`
- parsing helpers → `util.py`
- alert dedupe/cooldown pattern (`api/alerts.py`) → `alerts.py` + `ledger.alert_*`
- price-history dedupe (`api/market.py`) → `ledger.add_quote`
- keyword/category idea for sports filtering

Its `EXCLUDE_PATTERNS` that hid "X vs Y" matches is intentionally **not** carried over; matches are the point here.

## Known limits

- Kalshi has no exact kickoff field (`occurrence_datetime` runs ~3 h after start), so cross-venue matching uses team pair
  + a wide window. Kickoff precedence for closing capture: Odds API `commence_time` > Polymarket `gameStartTime` > Kalshi hint.
- Settlement rules differ by venue (90' vs. extra time, postponement windows). Arb alerts say "check rules".
- Paper fills assume the book doesn't move between snapshot and fill, and don't model queue position or latency.
- Unknown team names fall back to token containment. Add aliases to `clv_engine/data/team_aliases.json` when a game shows up unmatched in `scan`.
