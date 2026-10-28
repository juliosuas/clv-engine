# Graduation gate: paper -> real (PROPOSAL)

> **Everything below is a proposal for Julio to approve or change. None of it is a fact, a recommendation to
> trade, or a promise of edge.** `clv-engine` is paper-only: nothing in this repository places real orders,
> holds keys or touches a wallet, and no code here enforces the limits in section 3.

Run `clv-engine graduation` (or `--json`) to evaluate the gate against the ledger, and `clv-engine status` for
progress toward it. The weekly report prints both.

## 1. What counts as a "measured entry"

A paper entry that
- was priced against Pinnacle (`ref_source = pinnacle`) and has a **captured Pinnacle close** (`clv_status = done`;
  the close must be sampled within `CLOSE_MAX_AGE_MINUTES` of kickoff),
- is on Polymarket or Kalshi (soft-book entries are a different market and never count),
- was filled in full (no partial fills),
- and would pass today's entry rules (`MIN_EDGE`, not an excluded league/venue).

CLV = Pinnacle no-vig fair close / `effective_price` - 1, i.e. **after fees**.

## 2. The gate (all must hold; unknown or insufficient data counts as NOT met)

| # | Criterion | Default |
|---|-----------|---------|
| 1 | Measured entries | >= 300 (`GRAD_MIN_ENTRIES`) |
| 2 | Mean CLV | > 0 |
| 3 | Lower bound of the 90% CI of mean CLV (`GRAD_CI_LEVEL`) | > 0 under **both** a game-clustered percentile bootstrap and a t interval |
| 4 | After-fee paper P&L on the same sample | > 0, and >= 90% of the sample settled (`GRAD_MIN_SETTLED_FRACTION`) |
| 5 | No single league drives the result | >= 2 leagues with >= 30 entries, largest league <= 70% of the sample, mean CLV stays > 0 when any one league is removed |
| 6 | No single venue drives the result | >= 2 venues with >= 30 entries (`GRAD_MIN_VENUES`), each with mean CLV > 0 |

Notes
- The bootstrap resamples whole games (an entry on both sides or both venues of one game is not independent
  evidence). The plain t interval treats entries as independent and is optimistic, so the gate needs both.
- With the default `EXCLUDED_VENUES=kalshi` only one venue produces measured entries, so criterion 6 cannot pass
  until Kalshi is re-enabled or `GRAD_MIN_VENUES=1` is set. That is a choice for Julio.
- 300 entries is a proposed sample size, not a power calculation. A 90% CI that excludes 0 does not rule out
  luck across the many subsets one can slice; do not tune thresholds after looking at results.
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
