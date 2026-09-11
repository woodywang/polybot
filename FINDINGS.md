# Polymarket 5-minute crypto Up/Down — research log

A working log of what was measured, what turned out to be wrong, and what is
still open. Written so the reversals are visible: several early conclusions
here were overturned by later, larger samples, and the reasons are worth more
than the conclusions.

Everything below is measured against live data unless flagged as an estimate.

---

## 1. The account that started this

A widely shared thread described a bot on Polymarket's short BTC Up/Down
markets: ~50% win rate, ~20k predictions, ~$75k cumulative, ~$36 average
position, "Bayesian repricing + Kelly sizing".

The wallet is `0x41e2e1ccf1e4940029af02259a31c6b89b9fa354` (profile
`norm1e69`), resolved from the profile page and queried through the documented
Data API (`data-api.polymarket.com`, the host Polymarket's own docs describe as
"understand account and market activity"). 165,838 activity rows were pulled
covering 2026-08-28 → 09-11.

### What the raw ledger says

| Claim | Measured |
|---|---|
| $36 average position | **$13.31 mean, $5.67 median** |
| ~25 trades/hour | **~440 buy fills/hour** |
| ~20k predictions | 20k is *markets*, not fills; fills are ~20× that |
| 23.6 entries per market | 11.6 |
| BTC only | 87% of *notional* is BTC; by fill *count* ETH+SOL dominate |

Two structural facts the thread omitted:

- **99.99% of fills are taker** (147,942 taker vs 16 maker).
- **86.6% of markets (11,053/12,763) have both outcomes bought.** A hedged pair
  books one win and one loss, so the advertised "50% win rate" is substantially
  a bookkeeping artifact of pairing, not a property of the model.

### Real P&L decomposition (14 days)

```
turnover                      $1,964,563
gross trading edge            +$65,991     3.36% of turnover
  - taker fees                 -$46,573    70.6% of the gross edge
  + taker rebates              +$13,725
  = TRUE NET                   +$33,144     1.69% of turnover
```

Per share: **gross edge 1.64¢, fee 1.16¢, rebate back 0.34¢, net ~0.82¢.**

The leaderboard's all-time "+$82,890" does not reconcile with realized cash
flow and should not be read as withdrawable profit.

---

## 2. The fee schedule, verified fill by fill

```
fee = shares × 0.07 × p × (1-p)
```

Matched to 5 decimal places on every one of 148k fills. As a fraction of
capital deployed this simplifies to a result worth memorising:

```
fee as % of stake = 7% × (1 - p)
```

| price | fee/share | % of stake |
|---|---|---|
| 0.45 | 1.73¢ | **3.85%** |
| 0.50 | 1.75¢ | 3.50% |
| 0.90 | 0.63¢ | 0.70% |
| 0.99 | 0.07¢ | 0.07% |

The fee peaks exactly at the money — the operating point of any "50% win rate"
strategy. It was introduced in January 2026 explicitly to curb latency
arbitrage, and it is well designed for that.

### Taker rebates are a tiered refund, not a revenue source

`wV = trade size × (1 - entry price) × category weight` (crypto = 2.3). Divide
by the fee formula and the price cancels:

```
wV / fee = 2.3 / 0.07 = 32.857   (crypto, independent of price)
```

So tier thresholds translate directly into a required fee spend:

| tier | 30d wV | fee spend needed | rebate | level-up bonus |
|---|---|---|---|---|
| Gold | $200K | $6,087 | 18% | $250 |
| Platinum | $1M | $30,435 | 32% | $1,500 |
| Diamond | $4M | $121,739 | 44% | $7,500 |
| Obsidian | $10M+ | $304,348 | 50% | $25,000 |

Measured daily `rebate / fee` for the account was **exactly 18.0%** through
Sep 5 and **exactly 32.0%** from Sep 7, with a one-off **$1,500** on Sep 6 —
the documented Platinum level-up bonus. The account tripled turnover on
Sep 5-6 while crossing the tier.

**The rebate is capped at 50% of fees.** It cannot be farmed for profit on its
own; it is a fee discount that amplifies an already-positive edge.

---

## 3. Settlement is an average, not a spot price

Markets resolve on Chainlink's `btc-usd-twap-60s` stream, comparing the
window's TWAP against the price at the window's open. The payoff is a digital
on an **average**, so the textbook spot digital is the wrong model:

```
tau >= w:   mean = S,                      var = sigma^2 (tau - 2w/3)
tau <  w:   mean = (I_known + S*tau) / w,  var = sigma^2 tau^3 / (3 w^2)
```

Two effects pull in opposite directions, and the naive model has both wrong:

```
ATM rally : twap 0.4818 vs naive 0.5420  -> naive overpays Up  +0.0602
ITM late  : twap 0.9888 vs naive 0.8541  -> naive underpays Up +0.1347
same spot, same tau, different path: 0.4276 .. 0.7081  (spread 0.2805)
```

That third line is the edge: **path dependence worth 28 cents that a spot model
is structurally blind to.**

Chainlink Data Streams requires credentials, so Binance + Coinbase are
consolidated by median as a stand-in for its cross-venue aggregation. The
on-chain Chainlink feed (27s heartbeat, too slow to trade) is polled only to
measure what that costs: **mean |basis| ≈ 5.3 bps ≈ $4 on BTC.**

---

## 4. Bugs the live run found — all of them mine

Listed because each one produced a *confident* wrong answer, which is worse
than an obvious failure.

1. **Strike sampled as a point price.** The settlement source is itself a 60s
   TWAP, so the value published at the open is the mean of the preceding
   minute, not the tick on the boundary. Being wrong by a minute of drift,
   combined with variance collapsing as `tau^3`, produced confident losing
   trades: log-loss 1.44 and −22% P&L. Using the trailing 60s mean as the
   strike moved log-loss to 0.30.
2. **Order book silently froze.** `price_change` carries `price_changes`, not
   `changes`, and `asset_id` lives on each entry rather than the envelope. Read
   it wrong and the book stays on its opening snapshot while every quote goes
   stale. No error is raised.
3. **Event loop saturated.** One `create_task` per signal per 250ms tick
   produced thousands of pending tasks; Polymarket dropped the socket as a
   *slow consumer*. Fixed by allowing one in-flight order per (market, side).
4. **Sigma inflated by microstructure noise.** Sampling 1-second returns
   measures bid-ask bounce as much as price movement. With true sigma 0.50, 1s
   sampling estimated 0.81; 5s sampling gives 0.63. The inflated sigma widened
   the distribution and made the model systematically under-confident.
5. **Average-cost matching leaked backwards.** Hedged shares kept averaging
   with later, worse opens, so a run with a 1.5¢ minimum lock still paid
   $1.0084 per dollar. Fixed with FIFO lots that retire on match.
6. **Gamma's multi-slug query returns `[]` when combined with `closed=true`.**
   Batched settlement lookups therefore read as "nothing has settled" forever.
   One slug per request. Polymarket also flips `closed` about 4-6 minutes after
   a window ends, so earlier polls legitimately find nothing.
7. **Silent `str.replace` no-ops.** Several patches reported success while
   matching nothing, so guards that were believed active had never run. Every
   change is now grep-verified after writing.

---

## 5. What the strategy actually is

### Simultaneous arbitrage does not exist

Up and Down are complementary, so `ask_up + ask_down >= 1 + spread` at every
instant. Measured over **17,049 quotes: minimum two-sided cost $1.0011, zero
quotes below $1.00.** Not rare — absent.

### The sequential "lock" is not arbitrage either

A pair only comes in under a dollar if the price moved between the two legs:

```
lock beats holding  <=>  1 - ask - fee > p  <=>  (1 - p) > ask + fee
```

i.e. **only when the hedge leg is itself a positive-edge purchase.** Locking
whenever the pair is merely under $1 fires on winners too.

### The decomposition that matters

Paired and naked legs are not two strategies. They are the two branches of one
bet: the price moves toward leg 1 (hedge available, lock in ~+9%) or away from
it (hedge unavailable, leg expires worthless). Reporting them separately makes
the winning branch look risk-free.

```
breakeven  <=>  naked capital share  <  pair margin / (1 + pair margin)
```

| arm | lock rule | pair margin | needs naked < | actual | result |
|---|---|---|---|---|---|
| `paper_lock` | pair under $1, 1.5¢ | +9.09% | 8.34% | **11.2%** | −1.14% |
| `paper_patient` | pair under $1, 5¢ | +9.80% | 8.92% | 21.0% | −7.26% |
| `paper_dir` | hedge must be +EV | +20.01% | 16.67% | 50.0% | −10.25% |

`paper_lock` misses breakeven by **2.9 percentage points**. The pair margin is
already sufficient; the loss is entirely capital stranded in legs that never
found a hedge.

### The model has no directional edge

Hit rate across four independent samples: **42%–52%**, over 419 legs and 21
markets in the paired arms alone. An early 80% reading over 15 markets was a
single favourable hour — hour-to-hour return stdev is **13.6 points**, which is
large enough that any single-period comparison is uninformative and any fixed
percentage stop-loss would be triggered by normal noise.

### Naked residue loses mechanically, not by luck

`0/21` and `0/12` unhedged legs won. A leg fails to hedge precisely because the
price moved against it, so "could not hedge" and "already losing" are the same
event. The residue is not a random subsample.

### Force-closing the residue does not help

Settlement is automatic and binary ($1 or $0). A leg trading at $0.20 near
expiry already *has* expected value $0.20; selling it realises $0.20 minus
spread and fee, which is strictly worse. The money was lost when the price
moved, not at settlement.

---

## 6. Open questions

- **Does `--min-p-lock` close the 2.9-point gap?** The reflection principle
  gives `P(max_s W_s >= d) = 2*Phi(-d/(sigma*sqrt(tau)))`, so a leg's
  hedgeability is computable *before* opening it. Refusing legs that cannot be
  hedged attacks the naked share directly rather than trying to rescue it.
  Implemented; being tested as a fourth arm.
- **Is the ~9% pair margin stable across regimes?** Trend and amplitude are
  logged separately (Kaufman efficiency ratio and path length in bps) because
  one number cannot separate a 5bp drift with `er=1` from a 200bp swing with
  `er=0`. Not enough resolved markets per quadrant yet.
- **Does trade intensity predict anything?** Volume scales sigma as
  `sqrt(intensity)` under the mixture-of-distributions view. Early samples put
  high-volume median pair cost *above* low-volume, i.e. the wrong sign, on
  6 markets per bucket. Unresolved.
- **What is the drawdown envelope?** Under a random walk cumulative P&L is a
  martingale and max drawdown scales as `sigma*sqrt(n)`. A circuit breaker
  should be set against that envelope rather than a fixed percentage, but
  calibrating sigma on two hours of data would itself be overfitting.

---

## 7. How to reproduce

```bash
./scripts/env.sh python3 fair.py                     # model self-check
./scripts/env.sh python3 run.py --db paper.db        # collect
./scripts/env.sh python3 run.py --report --db paper.db
```

Everything runs in the container defined by `contrib/Dockerfile.buildenv`;
`scripts/env.sh` is the only entry point. No orders are ever sent — fills are
simulated against the displayed book after a configurable latency, with the
real taker fee applied. Live trading is one signed POST away and deliberately
absent until a calibration report says the edge clears the fee.
