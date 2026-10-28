# Graduation gate: paper -> real (PROPOSAL)

> **Everything below is a proposal for Julio to approve or change. None of it is a fact, a recommendation to
> trade, or a promise of edge.** `clv-engine` is paper-only: nothing in this repository places real orders,
> holds keys or touches a wallet, and no code here enforces the limits in section 3.

Run `clv-engine graduation` (or `--json`) to evaluate the gate against the ledger, and `clv-engine status` for
progress toward it. The weekly report prints both.

## 1. Pre-registered measurement rules (fixed before measuring; do not change after seeing results)

These are also in code (`graduation.PREREGISTERED_RULES`) and printed by `clv-engine graduation`.

1. **Close** = the last Pinnacle quote with timestamp <= the scheduled kickoff. If Pinnacle pulls the market before
   kickoff, the last valid sample (taken within `CLOSE_MAX_AGE_MINUTES` of kickoff) is used. If no valid sample exists the
   entry is marked `no_close` (after `SETTLE_AFTER_HOURS`), logged once, shown in `status`, and **excluded** from the sample
   (never filled in from another source).
2. **CLV** = Pinnacle no-vig fair close / all-in entry price - 1, where the all-in price includes the taker fee. The primary
   de-vig is the configured one; **power and multiplicative de-vig are both reported as sensitivity** (stored as closes
   `pinnacle_power` / `pinnacle_multiplicative`). The gate runs on the primary and flags a sign flip in either.
3. **Fills are always modelled as taker**: walk the asks, pay the taker fee. Maker/resting fills are never simulated as safe.
4. **The unit is the game**: entries of one game (both sides, both venues) are correlated, so the CI and t-stat use
   per-game means / a game-clustered bootstrap, and "n measured" = distinct games.

A *measured entry* is therefore one that was priced against Pinnacle (`ref_source = pinnacle`; consensus entries are not
independent of the venue prices and never count), has a captured Pinnacle close (`clv_status = done`), is on Polymarket or
Kalshi, was filled in full, and passes today's entry rules (`MIN_EDGE`, not an excluded league/venue).

## 2. The gate (all must hold; unknown or insufficient data is NOT met)

The gate has three outcomes, never two:
- **EXTEND (insufficient sample)**: fewer than `GRAD_MIN_GAMES` distinct measured games. Nothing is decided, keep collecting.
  This is not a failure, and any number printed before the threshold is provisional.
- **FAIL**: the sample is large enough and at least one criterion below is not met.
- **PASS**: all criteria are met.

| # | Criterion | Default |
|---|-----------|---------|
| 1 | Measured games | >= 300 (`GRAD_MIN_GAMES`), else EXTEND |
| 2 | Mean CLV (per-game means) | > 0 |
| 3 | Lower bound of the 90% CI of mean CLV (`GRAD_CI_LEVEL`) | > 0 under **both** a game-clustered percentile bootstrap and a t interval over per-game means |
| 4 | After-fee paper P&L on the same sample | > 0, and >= 90% of the sample settled (`GRAD_MIN_SETTLED_FRACTION`) |
| 5 | No single league drives the result | >= 2 leagues with >= 30 games (`GRAD_MIN_GROUP_GAMES`), largest league <= 70% of the sample, mean CLV stays > 0 when any one league is removed |
| 6 | No single venue drives the result | >= 2 venues with >= 30 games (`GRAD_MIN_VENUES`), each with mean CLV > 0 |

Pace: `status` shows the real entries/day under the current filters (computed retroactively on stored quotes when data
exist, re-applying every filter except `MIN_DEPTH_USD`, so an upper bound) and warns when the pace will not reach
`GRAD_MIN_GAMES` measured games before `GRAD_DEADLINE` (default 2027-01-27).

Notes
- With the default `EXCLUDED_VENUES=kalshi` only one venue produces measured entries, so criterion 6 cannot pass
  until Kalshi is re-enabled or `GRAD_MIN_VENUES=1` is set. That is a choice for Julio.
- 300 games is a proposed sample size, not a power calculation. A 90% CI that excludes 0 does not rule out
  luck across the many subsets one can slice; do not tune thresholds after looking at results.
- `venue_clv_pct` (own-venue close mid / avg price - 1) is **not CLV**: it is gross of the fee, it is the venue's own price
  drift, and for consensus-referenced entries it is not independent of the reference. It never enters the gate.
  `clv-engine audit` decomposes the gap between the declared edge and it.
- Passing means "the paper evidence meets the proposed bar", not "trade".

## 3. Proposed risk limits for a real phase (PROPOSAL, not implemented)

| Limit | Proposal |
|-------|----------|
| Bankroll | Small: an amount Julio can lose entirely. Julio sets the number (not chosen here). |
| Max per ticket | 1% of bankroll |
| Daily stop-loss | 3% of bankroll realized loss in a UTC day, then stop until the next day |
| Total stop-loss | 20% drawdown from the starting bankroll, then stop and re-review |
| Kill switch | One flag (file or env var) checked before every ticket that halts all real-money activity; plus a manual off |
| Scope | Only venues/leagues that individually clear the gate; start with the smallest ticket size |

The daily/total stop-loss percentages are placeholders to discuss; the 1%-per-ticket cap and the kill switch are
the requested parts. Any real-money step needs Julio's explicit approval and separately written and reviewed
code (wallet/keys/orders), which does not exist here and must not be added to the paper tracker.

## 4. Unverified inputs

- The Odds API plan size and quota reset period are unverified; `ODDS_API_DAILY_BUDGET` is a planning default.
  Collection speed (entries/day, hence time to 300) depends on it.
- Any live performance figure quoted by others (e.g. Togi's) is unverified here; this repo only reports what its
  own ledger measured.
