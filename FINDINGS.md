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

---

## 8. Out-of-sample replication — and what it killed

Features were scored by AUC on whether a leg ever found a hedge, then the same
scoring was repeated on an independent arm running a different lock policy over
the same feed. Only two of six survived:

| feature | `paper_lock` | `paper_dir` | verdict |
|---|---|---|---|
| ask price | 0.803 | 0.717 | replicates |
| best-ask depth | 0.352 | 0.370 | replicates (inverted) |
| trend ER | 0.710 | 0.527 | collapses |
| trade intensity | 0.350 | 0.476 | collapses |
| time remaining | 0.569 | 0.505 | collapses |
| moneyness | 0.460 | 0.487 | null in both |

**Why the survivors survived: they are leg-level, the casualties are
market-level.** Trend ER and trade intensity are measured once per window and
shared by every leg in it, so 864 legs carry the information of 81 markets —
an order of magnitude less than the leg count suggests. Confidence in any
market-level feature has to be discounted to the market count. This is the
single most useful methodological lesson so far, and it retired a filter that
had already been deployed.

### The ask filter raises the hedge rate but is not alpha

Comparing each price bucket's realised win rate against the probability its own
price implies:

| ask bucket | `paper_lock` excess | `paper_dir` excess |
|---|---|---|
| 0.40–0.50 | **+23.1 pt** | **−24.1 pt** |
| 0.50–0.60 | +11.7 pt | +3.1 pt |
| all legs | +3.4 pt | −7.0 pt |

The mid-book buckets flip sign between arms. Buying the favoured side makes a
hedge more likely to appear; it does not make the trade mispriced.

### Longshots are a replicated, mechanical loser

| ask bucket | win rate | implied | legs |
|---|---|---|---|
| 0.00–0.10 | **0.0%** | 4.5–4.9% | 117 |
| 0.10–0.20 | **0.0%** | 14.6–14.7% | 84 |

201 legs across both arms, every one worthless at settlement. Two forces point
the same way: the fee is `7% × (1-p)` of stake, so 6.3% at a dime, and a sigma
estimated even slightly high pushes probability into exactly the tail being
bought. Cheap contracts buy the model's least reliable estimate at its most
expensive fee.

### Corrections to earlier sections

- "Naked residue loses mechanically" was a small-sample artifact. At 81 markets
  naked legs win 26–39% and recover ~64% of their cost, so the breakeven rule in
  section 5 is conservative rather than exact.
- At 81 markets `paper_lock` is **+1.82%** and hedging helped both arms
  (+$50.62 and +$241.55). The directional hit rate is still 43–51%.

---

## 9. What the edge actually is: the short-dated variance risk premium

Inverting the model against the book gives the volatility the quotes themselves
assume. Comparing that with the volatility the tape delivered, across 156
settled markets and **replicated independently in both arms**:

| tercile | markets | return | |
|---|---|---|---|
| implied ≫ realised | 52 | **+10.4%** | the book prices more doubt than there is |
| middle | 52 | −4.3% | |
| implied ≪ realised | 52 | **−8.8%** | the book is more certain than it should be |

`paper_lock` r = −0.280 (95% band ±0.225), `paper_dir` r = −0.381 (±0.219).
Both past their own band, same sign, on arms running different lock policies.

**This is the opposite of gamma scalping.** A long-gamma rehedger earns
(realised − implied); this earns (implied − realised). The position is not
harvesting movement, it is selling overpriced doubt — the short-dated variance
risk premium, on the shortest-dated option that exists.

The mechanism also unifies two findings that looked unrelated:

- implied ≫ realised: quotes sit near 0.50 while the outcome is already
  decided, so the favoured side is underpriced and buying it wins.
- implied ≪ realised: quotes run to the extremes, so longshots look cheap and
  are not. **This is exactly the 201 legs bought under $0.20 that all settled
  worthless.** Longshot losses and the VRP are the same phenomenon.

It also demotes the ask filter: buying the favoured side is a crude proxy for
"the book is underpricing the favourite", which is why it raised the hedge rate
without producing alpha of its own.

Crucially this is a **per-observation** feature, not a per-window one — which
is why it replicated where trend and intensity did not (section 8).

### The instrument had to be fixed first

The first run of this test reported the same direction with a 0.04 median
volatility ratio, which is not a plausible number. 11.8% of inversions were
walking to a bound: no sigma can reprice a quote sitting on the opposite side
of even money from spot, so the bisection ran out and returned 10000.
`implied_sigma` now verifies its own answer and returns None instead. That
disagreement is itself information — the book and the model differ on which
side is favoured — but it is not a volatility. Convergence is 90.8%.

---

## 10. The anomaly has a name: favourite-longshot bias

Comparing the favoured side's realised win rate against the probability its own
quote implies, across 1,762 observations:

| time left | obs | favourite wins | its quote implies | excess |
|---|---|---|---|---|
| 240–300s | 435 | 69.2% | 62.3% | **+6.9 pt** |
| 180–240s | 419 | 83.1% | 68.1% | **+15.0 pt** |
| 120–180s | 412 | 83.0% | 74.3% | **+8.7 pt** |
| 60–120s | 355 | 87.3% | 79.3% | **+8.0 pt** |
| 30–60s | 122 | 92.6% | 78.8% | **+13.8 pt** |

The favourite beats its own price at **every** horizon. This is the
favourite-longshot bias, among the best documented anomalies in betting and
prediction markets: longshots are overbought, favourites are underbought.

Two things make this an understatement rather than an overstatement. The
implied column uses the **ask**, which sits above fair value, so the true gap is
wider. Against that, observations inside one market share its outcome, so the
effective sample is the ~81 markets, not the 1,762 rows.

### The VRP measure locates where the bias is largest

| VRP tercile | obs | favourite wins | implied | excess |
|---|---|---|---|---|
| low (implied ≈ realised) | 587 | 82.8% | 74.1% | +8.7 pt |
| middle | 588 | 78.1% | 76.6% | +1.5 pt |
| **high (implied ≫ realised)** | 587 | 82.8% | 62.6% | **+20.2 pt** |

Everything found so far is one anomaly seen from three sides: longshots under
$0.20 settling worthless, favourites carrying positive excess, and the VRP
ratio marking when the gap more than doubles. The arm the evidence points at is
favourites **and** rich implied vol together, which is now running.

### Inconclusive, recorded so it is not re-run as if new

When the book and the model disagree about which side is favoured, spot's
direction was right 58.3% (n=12) and 56.2% (n=16). At those sizes a coin lands
that far off routinely. Nothing is built on it.

---

## 11. Risk: the edge is not yet significant, and losses cluster

Per-market returns from the 87 settled markets in `paper_lock`:

```
mean +4.26%   stdev 26.01%   Sharpe per market +0.164
standard error 26.01/sqrt(87) = 2.79%   ->   t = 1.53
95% lower bound on the mean: -1.21%
```

**A negative edge cannot be ruled out.** Kelly off this mean would ask for
`mu/sigma^2` = 71% of bankroll per market, and even a quarter of that is 18% —
all of it betting estimation error. Reaching t = 2 at the observed effect size
needs about **150 settled markets**; there are 87. Until then position size is a
fixed small fraction, not a function of the estimated edge.

### Drawdowns are worse than the iid null

Bootstrapping 20,000 random reorderings of the same per-market returns:

| | `paper_lock` | `paper_dir` |
|---|---|---|
| realised max drawdown | −224.5% | −745.0% |
| shuffled p50 / p95 / p99 | −120.9 / −195.7 / −232.2% | −549.4 / −780.1 / −884.4% |
| percentile of the realised value | **1.4%** | 7.9% |

`paper_lock`'s drawdown is worse than 98.6% of reorderings of its own returns,
so **losses are serially correlated — bad stretches arrive in runs.** A limit
calibrated on an iid envelope is therefore too loose, which is why the live
breaker is a plain equity floor (`--max-dd`, default 25% off peak, stops opening
while hedging continues) rather than a sigma-root-n band.

(Drawdowns here are in summed per-market return points, so they scale with the
fraction risked per market rather than being an equity figure directly.)

### An environment bug that cost two launches

Two arms failed to start with every flag reported unrecognised, twice, and the
code was never at fault: **the session shell is zsh, which does not word-split
unquoted parameter expansions.** A variable holding the flag list arrived as one
argument. Args are written literally now.

---

## 12. Are the paper fills real? Measured, then charged

Every P&L here assumes the displayed ask is still there 250 ms later. Quote
persistence over 197k observations says that assumption is close to fair in the
mean but has a fat tail:

| latency | unchanged | improved | worsened | mean drift | p90 adverse |
|---|---|---|---|---|---|
| 250 ms | 79% | 11% | 10% | −0.009¢ | 4.00¢ |
| 500 ms | 70% | 15% | 15% | −0.020¢ | 5.00¢ |
| 2000 ms | 49% | 26% | 25% | −0.063¢ | 7.00¢ |

That is the **unconditional** figure, and it is the wrong one. The quotes worth
hitting are exactly the ones most likely to move. Splitting 5,356 samples by the
model's edge:

| model edge | samples | quote move after 250 ms |
|---|---|---|
| Q1 (most negative) | 1,071 | **−1.162¢** (in our favour) |
| Q3 | 1,071 | −0.002¢ |
| Q5 (most positive) | 1,072 | **+0.140¢** (against us) |
| would open (edge > 0.01) | 1,197 | **+0.130¢** |
| would skip (edge ≤ 0) | 3,762 | **−0.343¢** |

A 0.47¢ spread between the quotes the model wants and the ones it passes on.
Whether this is adverse selection or mean reversion in a low ask, the practical
consequence is identical: **the price that looks mispriced is not the price you
get.** At 0.130¢ against a ~1.6¢ gross edge it is about **8%** — real, far
smaller than the 4¢ p90 tail implies, and not disqualifying.

It is now charged rather than noted: `--slippage` (default 0.0013/share) is
added to every simulated fill, so all subsequent paper results are conservative
by the measured amount.

---

## 13. The robust half of the anomaly is the refusal, not the selection

Splitting the favourite-longshot result by asset weakens it considerably:

| asset | obs | markets | favourite wins | implied | excess |
|---|---|---|---|---|---|
| BTC | 740 | 31 | 78.5% | 69.7% | **+8.8 pt** |
| ETH | 731 | 31 | 77.4% | 68.8% | **+8.7 pt** |
| SOL | 658 | 31 | 69.6% | 70.5% | **−0.9 pt** |

**One asset in three shows nothing.** Any claim about buying favourites has to
carry that.

Per-share economics after the real fee and the measured slippage:

| favourite's price | obs | win rate | gross | fee | slip | net | on stake |
|---|---|---|---|---|---|---|---|
| 0.5–0.6 | 760 | 65.4% | +12.22¢ | −1.74¢ | −0.13¢ | +10.34¢ | **+19.5%** |
| 0.6–0.7 | 420 | 65.7% | +1.32¢ | −1.60¢ | −0.13¢ | −0.41¢ | **−0.6%** |
| 0.7–0.8 | 329 | 78.7% | +3.96¢ | −1.32¢ | −0.13¢ | +2.51¢ | +3.4% |
| 0.8–0.9 | 271 | 87.1% | +2.99¢ | −0.94¢ | −0.13¢ | +1.93¢ | +2.3% |
| 0.9–1.0 | 349 | 96.6% | +0.90¢ | −0.29¢ | −0.13¢ | +0.48¢ | +0.5% |

Positive on average but **not monotone** — the 0.6–0.7 bucket is negative. Weak.

The other side is the opposite:

| underdog's price | obs | win rate | net | on stake |
|---|---|---|---|---|
| 0.0–0.1 | 349 | 3.4% | −1.32¢ | **−30.5%** |
| 0.1–0.2 | 271 | 12.9% | −4.06¢ | **−25.5%** |
| 0.2–0.3 | 304 | 22.4% | −3.91¢ | **−15.7%** |
| 0.3–0.4 | 402 | 32.3% | −4.17¢ | **−12.0%** |

**Every bucket loses, monotonically in how cheap it is.** Because the fee is
`7% × (1-p)` of stake — 6.3% at a dime — buying the cheap side pays the most
expensive rate for the model's least reliable estimate.

So the durable rule is a refusal: **do not buy the cheaper side.** It survives
per-asset, it is monotone, and it needs no claim about alpha. Buying favourites
is the weaker half — positive on average, non-monotone, absent in SOL. This also
retires `--min-ask 0.25`: the 0.25–0.50 band loses at every step, so the line
belongs at 0.50.

### The arms are now a 2×2

| arm | favourite filter | VRP filter |
|---|---|---|
| `paper100_base` | — | — |
| `paper100_fav` | ask ≥ 0.50 | — |
| `paper100_vrp` | — | implied/realised ≥ 1.5 |
| `paper100_filt` | ask ≥ 0.50 | implied/realised ≥ 1.5 |

Each factor alone and both together, against a control, on one feed.

---

## 14. The model does beat the book — and most of the edge is in the last minute

The basic question, never asked until now: is the TWAP model better than simply
reading the quote? Log-loss against realised outcomes, 2,138 observations over
93 markets:

| time left | obs | TWAP model | naive digital | the book | coin flip |
|---|---|---|---|---|---|
| 240–300s | 540 | **0.6112** | 0.6116 | 0.6464 | 0.6931 |
| 180–240s | 513 | **0.4601** | 0.4669 | 0.5470 | 0.6931 |
| 120–180s | 480 | 0.4317 | **0.4286** | 0.5027 | 0.6931 |
| 60–120s | 420 | **0.3544** | 0.3645 | 0.4124 | 0.6931 |
| 5–60s | 185 | **0.1512** | 0.2278 | 0.3833 | 0.6931 |
| all | 2,138 | **0.4444** | 0.4541 | 0.5215 | 0.6931 |

Blending confirms it: 100% model is the best mix, and every weight on the book
makes the forecast worse. The model is not adding a little on top of the quote —
it strictly dominates it.

**The advantage concentrates in the final minute**, 0.1512 against the book's
0.3833, and that is exactly where the naive digital falls away too (0.2278).
The last minute is where the TWAP structure does its work: variance collapsing
as `tau^3` and most of the settlement average already printed.

This inverts the reading of section 10. The favourite-longshot bias is the
*symptom*; the cause is that **the book is slow to price the certainty a TWAP
settlement already implies near expiry.**

### A parameter of mine was blocking the best window

`--min-tau-open 45` was set early to leave time for a hedge to appear. Sorting
the legs the model would have opened:

| time left | legs | avg price | win rate | net/share | on stake |
|---|---|---|---|---|---|
| 45–60s | 48 | 0.550 | 70.8% | +13.97¢ | +25.4% |
| 20–45s | 42 | 0.478 | 78.6% | +28.93¢ | **+60.6%** |
| 2–20s | 28 | 0.552 | 96.4% | +39.35¢ | **+71.3%** |
| **blocked (< 45s)** | **70** | 0.507 | **85.7%** | **+33.09¢** | **+65.2%** |
| allowed (≥ 45s) | 1,141 | 0.587 | 69.1% | +8.58¢ | +14.6% |

Checked for the correlated-sample trap that has caught this project twice: the
70 legs come from **23 distinct markets**, 18 of which are net positive, and
aggregating per market gives +25.19¢/share with a 5.51¢ standard error —
**t = 4.57**, against t = 1.53 for the strategy overall. Depth is not the
obstacle either: median 80 shares at the best ask, about $54 tradeable against
a $3 order.

At 20 seconds with the model 78–96% confident, the hedge the parameter was
protecting is not needed. A fifth arm runs `--min-tau-open 5`.

---

## 15. The whole model loses to one comparison

Every rule scored the same way — pick a side, pay the ask, charge the real fee
and the measured slippage, aggregate per market so correlated legs cannot
inflate the t:

| rule | legs | markets | net/share | t |
|---|---|---|---|---|
| **spot above strike → buy Up, last 60s** | 210 | 65 | **+12.95¢** | **5.51** |
| TWAP model, edge > 0.01, last 60s | 126 | 36 | +12.02¢ | 2.33 |
| buy the favourite, last 60s | 210 | 65 | +5.06¢ | 1.69 |
| **spot above strike → buy Up, whole window** | 2,300 | 99 | **+7.17¢** | **3.14** |
| TWAP model, edge > 0.01, whole window | 1,292 | 96 | +3.39¢ | 1.11 |

The TWAP digital, the implied-vol inversion, the adaptive hedge band, Kelly,
the shrinkage — none of it beats *is spot above the strike*. The model's
direction agrees with spot in 2,180 of 2,181 samples, so all its filter does is
discard half the trades, and **the half it discards is the better half.**
Direction is what the model gets right; confidence is what it gets wrong.

### Why the spot rule also beats buying the favourite

They are the same trade 90.4% of the time. The difference is the other 9.6%:

| | legs | markets | win rate | net/share | t |
|---|---|---|---|---|---|
| book agrees with spot → buy it | 2,079 | 98 | 80.1% | +5.54¢ | 2.12 |
| they disagree → buy spot's side | 221 | 40 | 67.0% | +3.11¢ | 0.42 |
| they disagree → buy the book's side | 221 | 40 | **33.0%** | **−14.53¢** | −2.00 |

When the book disagrees with spot it is not merely uninformative, it is
**wrong** — 33% win rate and −14.53¢ a share. In the last minute the arms
disagree 16.7% of the time, which is why the spot rule (+12.95¢) pulls so far
ahead of the favourite rule (+5.06¢) there.

Following spot into the disagreement is not itself significant (t = 0.42). The
value is in *not following the book*, and the profitable core is the ordinary
agreement case in the last minute: 94.9% win rate, +10.92¢, t = 4.86.

### Two labelling errors caught before they became conclusions

`book_up = au < ad` had the sign backwards — a cheaper Up means the market
thinks Up is *less* likely — which swapped "agree" and "disagree" and would have
published the opposite mechanism. Earlier in the same pass, comparing a fill
price against the better of the two sides' quotes mixed the side bought with
the side that was not, producing a +11.55¢ slippage figure that was discarded.
Both were caught by numbers that did not look plausible (a 91.4% disagreement
rate; an 11¢ slippage), which is the only reason to keep checking magnitudes
against what the mechanism could physically produce.

---

## 16. The late-window rule is the first result that holds everywhere

The mistake made in section 8 — treating one favourable hour as a signal — has
to be checked against every claim since. Splitting the spot rule by hour:

| hour (UTC) | last 60s | | whole window | |
|---|---|---|---|---|
| | net/share | t | net/share | t |
| 14:00 | — | — | +24.36¢ | 8.29 (only 6 markets) |
| 15:00 | +5.42¢ | 2.06 | +3.76¢ | 1.24 |
| 16:00 | +26.30¢ | 5.69 | +16.98¢ | 4.69 |
| 17:00 | +12.25¢ | 2.56 | **−4.47¢** | −0.81 |
| all | **+14.12¢** | **5.81** | — | — |

**The last-minute rule is positive in every hour at t ≥ 2.06; the whole-window
version has a losing hour.** Concentrating late is not only higher return, it is
the difference between a result that repeats and one that does not.

Per asset, last 60s:

| asset | legs | markets | net/share | t |
|---|---|---|---|---|
| BTC | 74 | 21 | +7.80¢ | 1.76 |
| ETH | 88 | 24 | +18.30¢ | 5.17 |
| SOL | 57 | 22 | +15.60¢ | 3.42 |

All three positive — including SOL, where the favourite-longshot bias was
absent entirely (section 13). The spot rule survives the asset split that the
favourite rule failed. BTC at t = 1.76 is the weak one.

Three hours and roughly 20 markets per hour is still not much.

### A capacity estimate that is not reportable

Measuring depth at the best ask when the rule fires gives 76 shares (~$54)
median in the last minute, $15.90 at the 25th percentile. Scaling that to
36 markets an hour produced $904/hour on $1,976 deployed — 46% an hour, which
is obviously wrong. It breaks in three places: per-market win rates are 0 or 1
because observations inside a window share an outcome, so multiplying by depth
and averaging lets a few deep winners dominate; taking the full displayed size
ignores walking the book; and the extrapolation assumes $47k/day of aggressive
taking moves nothing.

What the measurement does support, and all it supports: **at $3 a fill the
strategy is nowhere near depth-constrained** (about 15% of median best-ask
size). Capacity beyond a few hundred dollars per market is unmeasured.

---

## 17. Refining the rule — and what a 100% win rate actually means

Last 60 seconds, split by how far spot sits from the strike:

| \|S−K\| | legs | markets | win | avg price | net/share | t |
|---|---|---|---|---|---|---|
| 0–2 bps | 49 | 19 | 73.5% | 0.693 | +4.06¢ | 0.47 |
| 2–5 bps | 48 | 23 | 95.8% | 0.843 | +10.63¢ | 4.28 |
| 5–10 bps | 36 | 22 | 100% | 0.836 | +11.85¢ | 3.52 |
| 10–20 bps | 44 | 14 | 100% | 0.680 | +21.13¢ | 3.32 |
| 20+ bps | 51 | 13 | 100% | 0.566 | +33.57¢ | 5.26 |

Under 2 bps the comparison is noise — 73.5% and t = 0.47. Everything the rule
earns comes from being decisively on one side.

By price paid, same window:

| ask | legs | win | net/share | on stake |
|---|---|---|---|---|
| 0.50–0.70 | 98 | 92.9% | +29.45¢ | **+55.7%** |
| 0.70–0.85 | 22 | 90.9% | +13.39¢ | +17.2% |
| 0.85–0.95 | 32 | 93.8% | +5.89¢ | +6.6% |
| 0.95–1.00 | 62 | 100% | +1.83¢ | +1.9% |

Combined (gap ≥ 5 bps, price ≤ 0.95): 93 legs, 23 markets, +33.55¢, t = 8.47.

### Why the 100% is not a guarantee, and why it is dangerous

A 100% leg-level rate here means **23 of 23 markets won**, since legs inside a
window share an outcome. At a 93% base rate, 23 straight is p ≈ 0.19 — ordinary.

Mechanically it must break. 5 bps on BTC at $77k is **$3.85**, while a
60-second move at the measured sigma is around **$23**. The gap does not lock
anything; the run of wins is luck riding a genuine but partial edge.

The real hazard is in the statistic: **a rule with no observed losses has an
unmeasured loss distribution.** t = 8.47 is high *because* the per-market
variance estimate is near zero, not because the edge is certain. Sizing off it
would be the same error as Kelly off an insignificant mean (section 11), except
worse — there the variance was merely large, here it is unestimated. The first
loss will move that t a long way.

Two arms now run the rule, one plain and one with `--min-gap-bps 5
--max-price 0.95`, so the refinement earns its place or does not.

---

## 18. RETRACTION: the late-window edge was a frozen order book

Sections 14, 16 and 17 are wrong. The quotes they were measured against stop
updating near expiry.

Rate at which a book changes between consecutive 250 ms snapshots:

| time left | snapshots | changed |
|---|---|---|
| 180–300s | 46,623 | 24.5% |
| 120–180s | 23,142 | 27.2% |
| 60–120s | 22,563 | 29.1% |
| 30–60s | 10,950 | 18.8% |
| **2–30s** | 10,206 | **4.9%** |

And for individual markets it is not merely slow, it is stopped: three examined
markets logged **194, 217 and 182 consecutive snapshots in their final minute
with zero quote changes.**

The raw rows make the artifact obvious. Of 14 samples with a ≥10 bps gap and
under 30 seconds left, twelve show `ask_Up ≈ 0.51 / ask_Dn ≈ 0.50` — a book
parked at even money while spot sits 13–49 bps away. That is not a mispricing
anyone would fill, it is a snapshot that stopped arriving. It also explains the
one anomaly that never made sense: the book's implied probability *falling back*
toward 0.50 near expiry (0.794 → 0.638 → 0.549). It was not falling, it had
stopped.

**Killed by this:** the last-minute concentration, `--min-tau-open 5`, the
distance refinement, the 100% win rates, t = 5.51, t = 8.47, +33.55¢/share.

### What survives, recomputed on live quotes only (tau ≥ 60s, 29.1% change rate)

| rule | legs | markets | win | net/share | t |
|---|---|---|---|---|---|
| **spot rule** | 2,221 | 105 | 77.6% | **+7.12¢** | **3.03** |
| buy the favourite | 2,221 | 105 | 74.0% | +4.47¢ | 1.67 |
| TWAP model, edge > 0.01 | 1,247 | 102 | 70.8% | +4.33¢ | 1.40 |

The core result holds: **a one-line spot comparison still beats the whole
model.** Per asset, BTC +9.06¢ (t 2.36) and ETH +10.20¢ (t 2.53) work, SOL
+2.09¢ (t 0.48) does not — the same asset split as section 13.

The distance refinement does not survive at all: 2–5 bps (t 1.90) is as good as
10+ bps (t 1.56), and the monotonic ladder was entirely an artifact of frozen
books at large gaps.

### The fix is a guard, not a parameter

`Book.stale_for()` now tracks when a book last moved and `--max-stale` (20s)
refuses to trade one that has not. Setting `--min-tau-open 60` would have hidden
this particular case; the guard catches the class, including a websocket that
silently stops delivering for a token after a resubscribe.

**How it was caught:** not by review but by pulling the raw rows behind an
aggregate that was too good. A 45-cent mispricing persisting for 18 seconds is
not something the mechanism can produce, and twelve rows reading 0.51/0.50 in a
row is not what real quotes look like.

---

## 19. The edge lives entirely in quotes that had stopped moving

Section 18 fixed the symptom with a `tau >= 60` cutoff. That was not enough.
Reconstructing, for every observation, how long its quote had been unchanged —
and the reconstruction is sound, since sampling is continuous (p99 gap 0.36s,
max 0.4s, no gaps over 2s):

| time left | samples | median stale | stale > 20s |
|---|---|---|---|
| 180–300s | 1,194 | 0.6s | **14.2%** |
| 120–180s | 544 | 0.6s | 18.8% |
| 60–120s | 483 | 0.6s | 18.4% |
| 30–60s | 188 | 0.8s | 31.4% |
| 2–30s | 43 | 282.1s | 72.1% |

Even well away from expiry, one observation in seven is against a quote that has
not moved in 20 seconds. Excluding them:

| filter | legs | markets | win | net/share | t |
|---|---|---|---|---|---|
| everything | 2,452 | 105 | 79.1% | +8.03¢ | 3.61 |
| tau ≥ 60s (section 18) | 2,221 | 105 | 77.6% | +7.12¢ | 3.03 |
| **stale < 20s** | 2,001 | 105 | 76.8% | **+0.68¢** | **0.27** |
| stale < 5s | 1,890 | 105 | 76.7% | +0.15¢ | 0.06 |

**The edge does not survive.** On quotes that are actually moving:

| rule | legs | markets | net/share | t |
|---|---|---|---|---|
| spot rule | 2,001 | 105 | +0.68¢ | 0.27 |
| buy the favourite | 2,001 | 105 | +4.37¢ | 1.75 |
| TWAP model | 967 | 97 | +0.15¢ | 0.05 |

None of the three is significant. Every earlier claim in this file about a
tradeable edge was measuring quotes that had stopped updating.

### The one question that decides it — and paper trading cannot answer it

Stale quotes are not random. They differ from live ones in exactly the way that
manufactures apparent edge:

| | samples | median \|gap\| | median depth | direction right |
|---|---|---|---|---|
| live (< 20s) | 982 | 5.9 bps | 124 sh | 76.7% |
| stale (> 20s) | 232 | **15.0 bps** | **389 sh** | **89.7%** |

A 389-share order sitting 15 bps from fair value for over 20 seconds is either

1. **real** — nobody is watching these thin 5-minute books, the order is
   genuinely there, and the edge is real but only reachable by actually sending
   an order; or
2. **an artifact** — the feed stopped for that token and the order is long gone.

Both are plausible. The feed does demonstrably stop (194, 217 and 182
consecutive unchanged snapshots), and equally, a resting order that far from
fair should have been taken if anyone were looking.

**No amount of further paper trading distinguishes them.** The next step is not
another arm or another filter: it is to send one small real order into a quote
flagged stale and record whether it fills. That single experiment decides
whether this entire strategy exists.

Until then the honest statement is: **on quotes verified to be live, there is no
measurable edge in any rule tested** — model, spot comparison, or favourite.

---

## 20. Answered without sending an order: stale books are not tradeable either way

Section 19 said paper trading could not tell a real resting order from a dead
feed. That was wrong — the CLOB REST book settles it. `stalecheck.py` watches
the websocket, and whenever a token's book has not moved for 20 seconds it pulls
`clob.polymarket.com/book` for the same token and compares.

26 probes, 20 with quotes on both sides:

| | count | |
|---|---|---|
| websocket matches REST | 12 | the order really is resting |
| websocket differs | 8 | **40% — the feed had gone quiet** |

And the two groups separate perfectly by whether they *look* profitable:

**Feed was dead** — every case a large apparent mispricing:

```
WS 0.63/0.64   REST 0.27/0.28     36¢ apart
WS 0.39/0.40   REST 0.70/0.71     31¢ apart
WS 0.64/0.65   REST 0.77/0.79     13¢ apart
```

**Order really frozen** — every case carries no edge:

```
WS 0.49/0.50   REST 0.49/0.50     at the money
WS 0.999/None  REST 0.999/None    already decided, 0.1¢ of upside
WS None/0.001  REST None/0.001     already decided
```

So a stale book is one of two things, and **neither is tradeable**: a dead feed
showing a mispricing that does not exist, or a genuinely frozen quote sitting
where there is nothing to win. The bigger the apparent edge, the more certain it
is the first.

This closes the question sections 18 and 19 left open, and it did not need a
live order — only a second, independent view of the same book. The lesson is
narrower than "paper trading cannot answer it": **a single data source cannot
audit itself.** Once the REST book was added as a cross-check the answer took
twelve minutes.

### Where that leaves the strategy

On quotes verified live by their own movement, no rule tested — the TWAP model,
the spot comparison, or buying the favourite — shows a significant edge
(section 19). The apparent edge was the feed, and the feed is now guarded
against by `--max-stale`. There is no evidence here of a tradeable strategy in
Polymarket's 5-minute crypto markets at retail latency.

---

## 21. The maker side is where the fee structure points

With no edge left on the taking side, the structure of the fees is worth
reading again. The spread is measurable from data already logged: Up and Down
are complementary, so `bid_up = 1 - ask_dn` and the Up spread is
`ask_up + ask_dn - 1`.

Over 215,473 snapshots it is **exactly one cent at every horizon** — p25, median
and p75 all 1.00¢ from 300s down to 60s. The book sits on the minimum tick
almost always.

Per share at the money:

| | maker | taker |
|---|---|---|
| half spread | **+0.50¢** | −0.50¢ |
| fee | 0 | **−1.75¢** |
| rebate (20% of taker fee, the conservative end) | +0.35¢ | — |
| **total** | **+0.85¢** | **−2.25¢** |

A **3.10¢ per share** swing between the two sides of the same trade — nearly
twice the 1.64¢ gross edge the real taker account was measured at in section 1.
It also closes that loop: that account paid 71% of its gross edge in fees, and a
maker doing identical volume keeps it.

### Adverse selection is real and appears to be covered

Mid-price changes are **positively autocorrelated, r = +0.0911** over 59,284
consecutive moves. Prices trend rather than revert, which is the bad direction
for a resting order:

| previous move | n | next move |
|---|---|---|
| up ≥ 1¢ | 23,122 | **+0.311¢** |
| up < 1¢ | 6,760 | +0.154¢ |
| down < 1¢ | 6,788 | −0.103¢ |
| down ≥ 1¢ | 22,614 | **−0.395¢** |

A maker filled on the bid is filled because someone is selling, and the mid then
falls another 0.10–0.40¢ on average. Netting that against the spread and rebate
leaves roughly **+0.45¢ to +0.75¢ a share** — still positive, against −2.25¢ for
taking the same trade.

### The variable that decides it cannot be measured from here

The spread is one cent, which is the minimum tick, so **there is no way to
improve a quote — only to queue behind it.** Median resting depth is 124 shares,
so a new order sits behind existing size and fills only when the queue clears,
which is precisely when the price is about to go through it.

**Queue position is the classic reason naive maker P&L is too optimistic, and
nothing in this data measures it.** The +0.45¢ to +0.75¢ has to be discounted by
an unknown factor, and testing it needs a different harness: signed limit orders
resting in a real book, which this one has never sent.

---

## 22. Four arms were still running without the stale guard

The guard added in section 18 was applied to one arm. The other four $100 arms
and both long-running arms kept trading frozen quotes for six more hours, and
their equity said exactly what a contaminated run says:

| arm (no guard) | markets | equity |
|---|---|---|
| base — no filters | 22 | **$142.85** |
| fav — ask ≥ 0.50 | 22 | $110.51 |
| vrp — implied/realised ≥ 1.5 | 22 | $113.50 |
| filt — both | 22 | $106.60 |

**The more the arm filtered, the less it "earned"** — because the filters were
already screening out some of the stale-quote trades that carried the artifact.
That is a third, independent confirmation of section 19, arriving by accident.

All four are archived as `contaminated_*.db` and restarted with `--max-stale 20`.
`paper_lock` and `paper_dir` keep running unguarded on purpose: they are the
historical dataset the staleness analysis was built on.

### First clean result, and it is "unknown" rather than "yes"

`paper100_spot`, the one arm that had the guard:

```
11 markets   stake $95.68   NET +$23.21   (+24.26%)
direction right 33/45 legs (73.3%)
per-market return: mean +24.52%, stdev 91.94%, se 27.72%
t = 0.88 -- not significant
markets needed for t = 2: about 56
```

+24% on eleven markets with a 92% per-market standard deviation says nothing.
It neither confirms nor refutes section 19; it is simply too small. Roughly five
more hours of collection reaches the sample where the question can be answered.

### A measurement refused

Queue dynamics were estimated from `obs` as a stand-in while the proper
diagnostic ran: 89.2% of adjacent samples showed the price had moved. **That
number does not measure what it looks like it measures** — `obs` samples every
20 seconds and books change on a sub-second scale, so of course the price moved.
It supports only the weaker claim that **a resting order at the best quote is
behind the market within 20 seconds about 89% of the time**, which is a warning
for the maker case but not the queue-clearing rate. That needs the 250 ms book
stream, which `queuecheck.py` collects.

---

## 23. A diagnostic retracted before it was published

The queue question from section 21 got a diagnostic, and it returned something
spectacular: of 9,114 best-bid levels, **0% ever cleared**, median lifetime
0.1s. Read literally that says a maker order never fills and the whole maker
case is dead.

It says no such thing. The code closed a level whenever the **best bid price
changed** — so when somebody bid *higher*, the level at the old price was
recorded as having died with its size intact, counted as "did not clear". The
0% is mostly that bug. What survives from it is narrow: the best bid is a very
unstable place (median 0.1s between changes) and a mean 21% of resting size is
consumed before a level stops being best.

This one was caught before it reached a conclusion, unlike the frozen-book
result, which ran for four sections first. The tell was the same both times —
a number too extreme for the mechanism to produce.

### The right measurement needs trades, and the feed has them

Sampling the websocket for 55 seconds across 12 tokens:

| event | count |
|---|---|
| `price_change` | 65,350 |
| `book` | 1,322 |
| **`last_trade_price`** | **602** |

Trades carry `price`, `size`, `side` and `asset_id` — roughly 0.9 per second per
token. A maker resting on the bid fills when a **taker sells** into it, so the
measurement is cumulative sell-side volume at or below the level's price against
the size a joiner would queue behind. The diagnostic is rewritten to that and
running.

### Arm status

| arm | markets | result |
|---|---|---|
| `paper100_spot` (guarded) | 14 with positions | +22.10%/market, **t = 1.02** |
| `paper100_base/fav/vrp/filt` | 0–3 | restarted with the guard, too young |
| `paper_lock` / `paper_dir` | historical, unguarded | +1.93% / −1.92% |

The guarded arm needs about **54 markets** for t = 2 and has 14. Nothing is
concluded from +22%; per-market standard deviation is 81%. **Sample
insufficient — no parameter changes made this round.**

---

## 24. With the guard on, four of five arms lose — by about their costs

First readings from the arms that actually refuse stale quotes:

| arm | markets | stake | net | return | fee | fee/stake | **gross (net + fee)** |
|---|---|---|---|---|---|---|---|
| base — no filter | 15 | $184.53 | −$11.37 | −6.16% | $6.83 | 3.70% | **−2.46%** |
| fav — ask ≥ 0.50 | 12 | $128.67 | −$8.10 | −6.30% | $4.21 | 3.27% | **−3.02%** |
| vrp — implied/realised ≥ 1.5 | 12 | $98.95 | −$19.56 | −19.77% | $3.21 | 3.25% | **−16.52%** |
| filt — both | 12 | $115.64 | −$6.99 | −6.04% | $3.90 | 3.37% | **−2.67%** |
| **spot — no model at all** | 30 | $313.46 | **+$27.59** | **+8.80%** | $10.96 | 3.50% | **+12.30%** |

Per-market t statistics: −1.69, −0.73, −1.69, −0.87, +0.72. **Nothing is
significant in either direction.**

Two things are worth reading anyway.

**The fee is 3.3–3.7% of stake in every arm** — exactly `7% × (1-p)` at prices
around 0.5–0.6. That is a certain, unavoidable drag, and it is larger than any
gross edge measured anywhere in this project except one.

**Before fees, the model-based arms are still negative** (−2.5% to −3.0%, and
−16.5% for the VRP filter). That is roughly the half-spread plus the measured
slippage, which is what a rule with zero alpha pays. The model's contribution is
not zero, it is slightly negative — it is selecting trades worse than picking at
random would.

The only arm with positive gross alpha is the one that uses no model: the spot
comparison, +12.30% before fees and +8.80% after. On 26 markets with a 64%
per-market standard deviation that is t = 0.72, so it is not evidence, only the
direction the rest of the file has been pointing.

### The queue diagnostic, third attempt

Versions one and two both closed a price level when the **best bid moved**,
which is not what happens to a resting order — somebody bidding higher does not
cancel yours. Median "level lifetime" came out at 0.04s and the fill rate near
zero, both artifacts of that clock.

The arithmetic says as much: 602 trades in 55 seconds across 12 tokens at ~8
shares each is roughly **220 sell-shares per minute per token**, against a
median of 20–45 shares resting ahead at the bid. A queue joiner should clear in
5–12 seconds if the price holds. Version three posts at the prevailing bid and
holds for a fixed 60 seconds, counting every sell at or below that price.
