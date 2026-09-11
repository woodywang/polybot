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

---

## 25. Guarded arms negative, unguarded arms positive — the cleanest demonstration yet

| | markets | return | t |
|---|---|---|---|
| **guarded** `base` (no filter) | 16 | −9.88% | **−1.94** |
| guarded `fav` | 15 | −1.61% | −0.21 |
| guarded `vrp` | 13 | −10.78% | −1.27 |
| guarded `filt` | 13 | −10.71% | −1.20 |
| guarded `spot` (no model) | 31 | +9.03% | +0.77 |
| **unguarded** `paper_lock` | historical | **+2.44%** | — |
| **unguarded** `paper_dir` | historical | **+1.30%** | — |

Same feed, same logic, one difference: whether the arm reads quotes that have
stopped moving. **Every guarded model arm is negative and both unguarded arms
are positive.** `paper_dir` was −1.92% a round ago and has since flipped
positive as more stale-quote trades accumulated.

`base` at **t = −1.94** is the closest anything in this project has come to
significance, and it points the wrong way: it needs 16 markets for t = 2 and has
15. The project's first significant result looks like it will be *trading this
model on live quotes loses money*, which is a finding about what not to do
rather than a strategy.

`spot` remains the only positive guarded arm at +9.03%, but its per-market
standard deviation of 63% puts the sample needed for t = 2 at **184 markets**
against the 31 it has. Nothing is concluded.

**Sample insufficient. No parameters changed, no new arm started.**

---

## 26. Re-auditing section 15 on live quotes: that finding goes too

Section 15 reported that when the book disagrees with spot about which side is
favoured, the book is not merely uninformative but **wrong** — 33.0% win rate,
−14.53¢ a share. Re-run with the staleness filter:

| | legs | markets | win | net/share | t |
|---|---|---|---|---|---|
| **all quotes (the original basis)** | | | | | |
| agree → buy that side | 3,033 | 146 | 77.6% | +3.28¢ | 1.52 |
| disagree → buy spot's side | 307 | 63 | 67.1% | −1.47¢ | −0.25 |
| disagree → buy the book's side | 307 | 63 | **32.9%** | −9.57¢ | −1.67 |
| **live quotes only (stale < 20s)** | | | | | |
| agree → buy that side | 2,755 | 142 | 75.9% | +1.50¢ | 0.72 |
| disagree → buy spot's side | 121 | 56 | **48.8%** | −7.58¢ | −1.22 |
| disagree → buy the book's side | 121 | 56 | **51.2%** | −4.52¢ | −0.75 |

On quotes that are moving, **the disagreement carries no information at all** —
51.2% against 48.8% is a coin, and both sides lose after costs. The 33% was the
stale feed: a frozen quote sits on the wrong side of the strike by construction,
because the price moved and the quote did not.

The disagreement *rate* itself falls from 9.2% to 4.2%, so **more than half of
every "disagreement" ever measured here was a stale book**.

The agreement case survives directionally but loses its significance too,
halving from +3.28¢ (t 1.52) to +1.50¢ (t 0.72).

This is the first systematic re-audit of an old conclusion against the
staleness filter rather than a new experiment, and it should have come
immediately after section 19. Every claim in sections 1–17 that rests on quote
data needs the same treatment; the ones already redone are 14, 16, 17 (retracted
in 18), the spot/favourite/model comparison (19), and now 15.

---

## 27. RETRACTION: the VRP conditioning was the stale feed wearing a different face

Sections 9 and 10 made the variance risk premium the centrepiece — the trade
pays when the book prices more uncertainty than the tape delivers, and the VRP
tercile located where the favourite-longshot bias more than doubled. Re-run
against outcomes directly, so no contaminated P&L enters:

| VRP tercile | obs | markets | favourite wins | implied | excess | net/share |
|---|---|---|---|---|---|---|
| **all quotes** | | | | | | |
| low (implied ≈ realised) | 1,037 | 128 | 76.3% | 73.1% | +3.2 pt | +1.81¢ |
| middle | 1,038 | 130 | 76.5% | 75.6% | +0.9 pt | −0.35¢ |
| **high (implied ≫ realised)** | 1,037 | 134 | 77.6% | 66.1% | **+11.5 pt** | **+10.04¢** |
| **live quotes only** | | | | | | |
| low | 936 | 125 | 75.7% | 72.7% | +3.1 pt | +1.69¢ |
| middle | 937 | 127 | 77.2% | 76.1% | +1.1 pt | −0.17¢ |
| **high** | 936 | 137 | 74.5% | 71.3% | **+3.2 pt** | **+1.77¢** |

**On live quotes the gradient disappears completely** — the high tercile returns
exactly what the low one does.

The mechanism is mechanical and obvious in hindsight. Implied sigma is inverted
from the **ask prices**. A frozen quote parked at 0.51/0.50 while spot has moved
far away inverts to an enormous implied volatility, because the book looks
maximally uncertain about an outcome that is not. **"High VRP" was largely a
synonym for "stale quote"**, and the stale quotes carried the fake edge. The
+20.2 pt of section 10 is the same artifact seen from a third angle.

### The first re-audit of this finding was a false positive

Section 26's method — filter the signal by staleness, keep the arms' per-market
P&L as the outcome — reported the VRP correlation surviving at r = −0.216, still
significant. That was wrong, because the P&L came from `paper_lock` and
`paper_dir`, which are **unguarded** and traded stale quotes themselves.
**Filtering the signal is not enough; the outcome variable has to be clean too.**
Using the market's own settlement instead of an arm's P&L removes the problem
entirely, and the effect vanishes.

### What is left standing

The favourite still beats its own quote by **+3.1 pt** on live quotes, worth
**+1.7¢ a share** after the real fee and the measured slippage, and it needs no
VRP condition. That is the whole of the surviving edge, and at 125–137 markets
it has not been tested for significance per market.

---

## 28. The maker case closes: the queue never reaches you

Third version of the diagnostic, with the clock finally right — post at the
prevailing best bid, hold a fixed 60 seconds, count every sell that trades at or
below that price:

```
270 simulated posts, 60s each
  queue ahead cleared within 60s     10  (3.7%)
  size ahead at post                 median 105 sh
  sell volume at that price in 60s   median 0 sh, mean 16 sh
  sold / size-ahead                  median 0.00, p90 0.05
```

**More than half of all posts see zero volume at their price for a full
minute.**

### The arithmetic in section 24 was wrong

That round estimated ~220 sell-shares per minute per token from 602 trades in
55 seconds, and concluded a joiner should clear in 5–12 seconds. The error was
counting *all* trades as if they hit the bid. A taker buying lifts the ask and
does nothing for a resting bid, and the sells that do arrive are spread across
price levels. The measured rate at a specific bid is **16 shares a minute, not
220**.

Which gives the number that closes this:

```
105 shares ahead / 16 shares per minute  ~=  6.6 minutes to clear
```

**The market only lives five minutes.** The queue does not reach you before the
contract settles.

### What that does to sections 14 and 21

The per-share maker arithmetic there was right — half spread plus rebate minus
adverse selection, roughly +0.45¢ to +0.75¢ against −2.25¢ for taking the same
trade. It assumed a fill. At a 3.7% fill rate inside 60 seconds the expectation
is **0.75¢ × 0.037 ≈ 0.03¢ a share**, before charging anything for the price
having moved while you waited.

Making does not work here for the same reason taking does not: not because the
economics are wrong, but because the market is too thin to transact on at all.
Every direction this project has explored now terminates in the same place.

---

## 29. The last surviving edge does not survive either

The one claim left standing — the favourite beats its own quote — tested the way
every claim should have been from the start: live quotes only, market settlement
as the outcome, aggregated per market.

| | legs | markets | win | excess | net/share | t | markets for t=2 |
|---|---|---|---|---|---|---|---|
| **all** | 3,000 | 153 | 74.2% | +1.6 pt | +1.94¢ | **0.95** | 674 |
| BTC | 1,054 | 51 | 77.0% | +4.3 pt | +4.97¢ | 1.61 | 78 |
| ETH | 1,027 | 51 | 75.4% | +3.3 pt | +5.25¢ | 1.49 | 92 |
| SOL | 919 | 51 | 69.6% | −3.4 pt | −4.41¢ | −1.16 | 152 |
| tau 180–300s | 1,528 | 153 | 69.6% | +3.8 pt | +3.38¢ | 1.40 | 311 |
| tau 120–180s | 674 | 130 | 75.5% | −1.2 pt | −2.54¢ | −0.87 | 689 |
| tau 60–120s | 586 | 126 | 79.0% | −1.5 pt | −3.54¢ | −1.31 | 293 |
| **tau 5–60s** | 212 | 81 | 89.6% | +3.6 pt | +3.48¢ | **2.06** | 76 |
| **price 0.50–0.65** | 1,106 | 145 | 61.0% | +3.8 pt | +5.99¢ | **2.37** | 103 |
| price 0.65–0.80 | 887 | 126 | 72.0% | +0.3 pt | +0.96¢ | 0.32 | 5,018 |
| price 0.80–0.95 | 674 | 126 | 86.9% | +0.5 pt | −4.63¢ | −1.63 | 191 |
| price 0.95–1.00 | 333 | 107 | 97.9% | +0.4 pt | −0.17¢ | −0.12 | 28,718 |

Overall: **t = 0.95, not significant**, and 674 markets would be needed.

Two subgroups clear t = 2. **Twelve tests were run.** Bonferroni puts the
threshold at α = 0.05/12 = 0.0042, which is **|t| > 2.86**. The largest observed
is 2.37. **Nothing survives the correction**, and finding two nominal hits in
twelve tests is what noise produces.

Reporting only those two would be data mining, and it is exactly the shape of
the error that produced every retracted section in this file: a subgroup with a
striking number, published before asking how many subgroups were looked at.

---

## The bottom line

Every route has now been measured and closed:

| route | outcome |
|---|---|
| simultaneous two-sided arbitrage | 17,049 quotes, none under $1.00 |
| sequential pair lock | not arbitrage — one branch of a directional bet |
| TWAP model timing | gross alpha **negative** on live quotes (−2.5% to −3.0%) |
| spot-vs-strike rule | +9.03% on 31 markets, t = 0.77, needs 184 |
| variance risk premium | retracted — stale quotes inverted to high implied vol |
| favourite-longshot bias | +1.6 pt, t = 0.95; no subgroup survives correction |
| market making | queue takes 6.6 minutes, the market lives 5 |

**No tradeable edge was demonstrated at retail latency in Polymarket's
five-minute crypto markets.** The honest output of this project is a negative
result plus the instrumentation that produced it — a staleness guard, a
cross-source book audit, a trade-based queue model, and the accounting that
reconciles to settlement.

The most expensive lesson is methodological. Six separate findings looked
strong, replicated, and had plausible mechanisms; all six were the same
artifact, a websocket that stops delivering. What caught it was not code review
but the discipline of pulling raw rows behind any number too good for the
mechanism to produce — and once a second data source was added, twelve minutes
settled what four sections of analysis had not.

---

## 30. A route never tested: the hourly markets have an exact strike

Everything above concerns the 5-minute markets, which settle on a Chainlink
TWAP-60s stream that needs credentials this project does not have. The **hourly**
markets settle on something else entirely:

> "resolve to Up if the close price is greater than or equal to the open price
> for the BTC/USDT 1 hour candle that begins on the time and date in the title"

That is a Binance candle. It is directly readable from the public klines
endpoint, which removes **all three** of the largest error sources at once:

| error source (5-minute markets) | hourly markets |
|---|---|
| venue basis, mean \|5.3\| bps | **zero** — settlement *is* Binance |
| TWAP-of-an-average approximation | **zero** — plain spot digital |
| strike integrated from 60s of ticks | **zero** — the candle's open, read exactly |

The model needs no change: with `--twap-w` near zero the TWAP digital's
`sigma^2 (tau - 2w/3)` collapses to `sigma^2 tau`, which is the spot digital.

Liquidity is a different world too. The current hour quoted $4,159 and the next
$25,765, against best-ask depths of a few hundred shares in the 5-minute books —
and it was thinness, not economics, that closed both the taking and the making
case (sections 19 and 28).

First live samples:

```
tau=2375  Up   ask=0.670  depth=177  fair=0.592  spot=77128.9  K=77054.0
tau=2374  Down ask=0.350  depth= 71  fair=0.597
```

Spot is 97 bps above an exactly-known strike with 40 minutes to run.

### Two patches that failed silently, again

The hourly strike block was written with `str.replace`, reported success,
matched nothing, and the arm ran for twenty minutes recording zero markets. Six
other strings were grep-verified and this one was not — the identical failure
recorded in section 4 as already learned. **Sampling the verification is not
verifying it.** A second bug survived the same way: one `mk["start"] + WIN`
left hard-coded against a 300s constant, which in hourly mode would have
computed the averaging window an hour out of place.

Both are now applied by `Edit` and every replacement checked, not a sample of
them.

---

## 31. The model works on the hourly instrument — 498 independent outcomes

Because hourly settlement is a public Binance candle, the model can be
backtested on history rather than on the 30 markets a live arm collects in an
hour. A week of 1-minute klines for BTC, ETH and SOL gives 29,382 observations
across **498 independent hourly outcomes**, scored against the exact settlement
condition:

| time left | obs | model log-loss | coin flip | improvement |
|---|---|---|---|---|
| 2700–3600s | 7,470 | 0.6340 | 0.6931 | 8.5% |
| 1800–2700s | 7,470 | 0.5519 | 0.6931 | 20.4% |
| 900–1800s | 7,470 | 0.4057 | 0.6931 | 41.5% |
| 300–900s | 4,980 | 0.2556 | 0.6931 | 63.1% |
| 30–300s | 1,992 | 0.1245 | 0.6931 | **82.0%** |
| **all** | 29,382 | **0.4564** | 0.6931 | **34.2%** |

This is the strongest statistical evidence in the project by a wide margin —
sixteen times the independent sample of anything measured live.

Calibration shows a consistent downward bias:

| model p | obs | predicted | actual | error |
|---|---|---|---|---|
| 0.1–0.2 | 2,042 | 0.15 | 0.106 | −0.044 |
| 0.3–0.4 | 2,874 | 0.35 | 0.261 | **−0.089** |
| 0.4–0.5 | 3,757 | 0.45 | 0.384 | −0.066 |
| 0.6–0.7 | 2,943 | 0.65 | 0.687 | +0.037 |
| 0.9–1.0 | 3,743 | 0.95 | 0.960 | +0.010 |

Below even money the model overstates Up; above 0.6 it slightly understates.
That matches the week's base rate — 48.5%, 46.1% and 47.9% Up for the three
assets — against a model that assumes zero drift.

**This is deliberately not corrected.** Fitting a drift term to one week of a
falling market is precisely the overfit that produced six retracted sections
here. The bias is recorded and left alone until the sample covers more regimes.

### The boundary this does not cross

**Predictive power is not edge.** This shows the model forecasts the settlement;
it does not show it beats the *quoted price*, and there is no history of hourly
quotes to test that against. The 5-minute model also "beat the book" by a wide
margin (0.4444 against 0.5215 in section 14) and that entire result turned out
to be frozen quotes. The live hourly arm is the only thing that settles it.

---

## 32. Hourly quotes do not freeze the way 5-minute ones do

The artifact that invalidated six findings was a book that stops updating. The
first thing worth knowing about the hourly instrument is whether it has the same
disease:

| dataset | snapshots | change rate | median stale | stale > 20s |
|---|---|---|---|---|
| **hourly** | 3,035 | 10.8% | 2.1s | **5.9%** |
| 5-minute (guarded arm) | 56,472 | 26.1% | 0.8s | 11.5% |
| 5-minute (historical) | 171,918 | 24.1% | 1.1s | 19.5% |

The hourly book changes *less often* and is *less stale* at the same time, which
is not a contradiction: a one-hour contract's fair value moves far less per unit
time than a five-minute one, so fewer updates are correct behaviour. What
matters is the tail — **5.9% of hourly snapshots sit on a quote older than 20
seconds, against 19.5% in the 5-minute history**, a third as much.

By time remaining, hourly:

| time left | samples | median stale | stale > 20s |
|---|---|---|---|
| 2400–3600s | 173 | 0.8s | **0.0%** |
| 1200–2400s | 2,865 | 2.4s | 6.3% |

One hour of data, so this is an indication rather than a measurement, but it
points the same way as the liquidity ($25,765 quoted against a few hundred
shares) and the same way as the exact strike. Every dimension that killed the
5-minute case is better here.

---

## Where twelve hours of work ends up

**The 5-minute markets: closed.** No tradeable edge at retail latency, by any of
seven routes, and the apparent edges were a websocket that stops delivering.
Both taking and making fail for the same underlying reason — the market is too
thin to transact on.

**The hourly markets: open, and untested.** They differ on every dimension that
mattered:

| | 5-minute | hourly |
|---|---|---|
| settlement source | Chainlink TWAP-60s (needs credentials) | Binance 1h candle (public) |
| strike error | estimated, ~5.3 bps basis | **exact** |
| model form | digital on an average | plain spot digital |
| quoted liquidity | a few hundred shares | **$25,765** |
| quotes stale > 20s | 19.5% | **5.9%** |
| model vs settlement | never cleanly measured | **34.2% better than a coin, 498 outcomes** |

What is still unknown is the only thing that decides it: **whether the model
beats the quoted price.** The 5-minute model appeared to, by a wide margin, and
that was the artifact. There is no history of hourly quotes, so the live arm is
the only instrument that can answer it.

The methodological residue is worth more than either result:

- a single data source cannot audit itself — the REST book settled in twelve
  minutes what four sections of analysis could not
- any number the mechanism cannot physically produce is an instrument fault
  until proven otherwise; that test caught the frozen books and three of the
  four broken diagnostics
- filtering the signal is not enough, the outcome variable has to be clean too
- twelve subgroup tests produce two nominal hits at t > 2 by construction
- verifying a sample of your patches is not verifying them

---

## 33. Thirty days: the model is stable and the calibration bias was noise

Extending the hourly backtest from one week to thirty days — **2,157
independent hourly outcomes**, 127,185 observations:

| week | hours | Up rate | log-loss | improvement | bias p<0.5 | bias p>0.6 |
|---|---|---|---|---|---|---|
| Aug 12 | 504 | 53.8% | 0.4737 | 31.7% | +0.027 | +0.040 |
| Aug 19 | 504 | 52.6% | 0.4622 | 33.3% | −0.011 | +0.004 |
| Aug 26 | 504 | 49.8% | 0.4803 | 30.7% | +0.016 | −0.012 |
| Sep 2 | 504 | 51.4% | 0.4518 | 34.8% | −0.036 | +0.029 |
| **all** | **2,157** | **51.3%** | **0.4683** | **32.4%** | **−0.002** | **+0.013** |

Two things settle here.

**The calibration bias was noise.** Section 31 measured the model overstating Up
below even money by up to 0.089 and left it uncorrected on the grounds that
fitting a drift to one falling week is the overfit that produced six retractions
in this file. Over thirty days the bias is **−0.002**, and week to week it flips
sign (+0.027, −0.011, +0.016, −0.036). The one-week base rate of 47.5% is 51.3%
over the month. Correcting it would have baked a week of noise into the model
permanently.

**The predictive power is stable, not a regime.** 31.7%, 33.3%, 30.7%, 34.8% —
four weeks spanning rising and falling markets, all within three points.

At 2,157 independent outcomes this is by a wide margin the best-supported claim
in the project, and it is worth being precise about what it claims:

> Given spot, the hour's open, time remaining and trailing realised volatility,
> the model forecasts the Binance hourly candle's direction 32% better than a
> coin, consistently across a month.

It still says nothing about whether that beats the **quoted price**, which is
the only question that pays. The 5-minute model beat the book by a similar
margin and the whole result was a frozen feed.

---

## 34. The decisive test, run on history: the model does beat the book on hourly markets

Polymarket serves quote history per token (`clob.polymarket.com/prices-history`,
1-minute fidelity) and hourly settlement is a public Binance candle, so the
question that could not be answered live is answerable from history. **432
resolved hourly markets, 25,396 observations**, model and book scored against
the same outcomes:

| time left | obs | model | book | coin | winner |
|---|---|---|---|---|---|
| 2700–3600s | 6,030 | 0.6371 | **0.6358** | 0.6931 | book |
| 1800–2700s | 6,468 | 0.5562 | **0.5544** | 0.6931 | book |
| 900–1800s | 6,431 | **0.4065** | 0.4139 | 0.6931 | model |
| 300–900s | 4,317 | **0.2544** | 0.2618 | 0.6931 | model |
| 30–300s | 2,150 | **0.1254** | 0.1432 | 0.6931 | model |
| **all** | 25,396 | **0.4497** | 0.4536 | 0.6931 | model |

The blend is the informative part:

```
  0% model (pure book)   0.4536
 60% model               0.4480   <- best
100% model               0.4497
```

**Neither dominates — they carry different information.** That is the opposite
of the 5-minute case, where 100% model was optimal, and that was the signature
of a frozen book rather than a good model.

### Trading the disagreement

Buying whichever side the model prefers when it differs from the quote by at
least 10 points, paying the quoted price plus the real fee and the measured
slippage, aggregated per market so correlated legs cannot inflate anything:

| split | legs | markets | win | net/share | t |
|---|---|---|---|---|---|
| **all** | 2,961 | 412 | 51.5% | **+9.47¢** | **8.49** |
| tau 1800–3600s | 1,356 | 384 | 50.5% | +5.30¢ | 3.08 |
| tau 900–1800s | 848 | 284 | 53.3% | +10.18¢ | 5.32 |
| tau 30–900s | 757 | 205 | 51.3% | +11.01¢ | 6.81 |
| BTC | 921 | 137 | 58.4% | +9.67¢ | 4.94 |
| ETH | 989 | 136 | 48.4% | +11.54¢ | 5.77 |
| SOL | 1,051 | 139 | 48.3% | +7.25¢ | 3.94 |
| Sep 5–7 | 860 | 138 | 50.7% | +9.13¢ | 4.72 |
| Sep 7–9 | 980 | 135 | 51.0% | +9.15¢ | 4.77 |
| Sep 9–11 | 1,121 | 139 | 52.5% | +10.13¢ | 5.17 |

Every split significant, and monotone in the threshold: +4.76¢ at 5 points,
+9.47¢ at 10, +14.64¢ at 15, +22.94¢ at 20.

### It is not the stale-price artifact

The obvious failure mode is that `prices-history` carries a last trade forward,
so the model "correctly" disagrees with a price nobody is quoting. It does not:

| price unchanged for | obs | markets | net/share | t |
|---|---|---|---|---|
| 0–1 min | 2,718 | 405 | **+9.59¢** | 8.58 |
| 1–3 min | 238 | 177 | +9.44¢ | 2.87 |

Only 0.6% of points are unchanged for five minutes or more, and restricting to
prices that *just moved* leaves the edge identical. Decisively, the
disagreement rate **falls** with staleness — 12.3% at zero minutes, 9.4% at one,
3.6% at two — where the artifact would make it rise.

### What is still unverified

`p` may be a mid or a last trade rather than an executable ask. If it is a mid,
the real ask sits roughly half a spread higher, and +9.47¢ has that much
headroom but the figure would shrink. Cross-checking `prices-history` against
the live book now recording in `paper_hour.db` is the same
two-independent-sources technique that settled the frozen-book question, and it
is the next thing to do.

---

## 35. The circuit breaker fired, on exactly the two arms it should have

First live trigger of the drawdown limit from section 24:

```
base  [HALT] drawdown 31.0% >= 25%; opening stopped, hedging continues
vrp   [HALT] drawdown 27.6% >= 25%; opening stopped, hedging continues
```

| arm | peak | now | drawdown | per-market t |
|---|---|---|---|---|
| `base` | $100.00 | $68.00 | 32.0% | **−1.99** (halted) |
| `vrp` | $104.93 | $83.68 | 20.2% | **−1.27** (halted) |
| `fav` | $110.33 | $107.32 | 2.7% | +0.94 |
| `filt` | $106.43 | $99.15 | 6.8% | −0.52 |
| `spot` | $142.26 | $142.26 | 0.0% | +1.42 |

**The two arms it stopped are the two with the most negative t statistics**, and
the design held: opening stopped while hedging continued, so no naked inventory
was stranded by the halt. That was the specific reason the breaker was written
as an equity floor on *opening* rather than a full stop.

A diagnostic note: the VRP arm's inactivity was first read as "the filter finds
nothing now that stale quotes are excluded", which would have been a neat
confirmation of section 27. It was wrong — 29.8% of its samples still meet
implied/realised ≥ 1.5. The arm had simply halted. **The tidy explanation was
available before the log was checked, which is how the six retracted findings
started.**

### Arm status

| arm | markets | return | t |
|---|---|---|---|
| base | 42 | −5.18% | −1.99 |
| fav | 36 | +3.85% | +0.94 |
| vrp | 36 | −10.78% | −1.27 |
| filt | 36 | −0.27% | −0.52 |
| spot | 54 | +10.55% | +1.42 |
| hour | 0 | — | hourly markets need an hour; 29 legs open |

Nothing significant. `spot` has moved from t = 0.77 to t = 1.42 over 50 markets,
the same direction but nowhere near the 184 it needs.

**Sample insufficient on every live arm — no parameters changed.** The evidence
that matters this round is historical, not live: 432 resolved hourly markets at
t = 8.49, and it points at the hourly instrument rather than at any of these
five.

---

## 36. The price in that history is a mid, and the edge survives paying the ask

The open question on section 34 was whether `prices-history` returns something
executable. Cross-checked against the live book recorded in `paper_hour.db` —
the same two-independent-sources method that settled the frozen-book question —
on 50 timestamp-aligned pairs:

| comparison | median | mean abs |
|---|---|---|
| p − bid | +0.50¢ | 1.42¢ |
| p − ask | −1.50¢ | 2.18¢ |
| **p − mid** | **+0.00¢** | **1.15¢** |

**It is the mid.** The backtest was therefore buying at mid, not at a price
anyone would fill. The hourly spread measured over 12,320 live snapshots is
2.00¢ median (p25 1.00¢, p75 3.00¢), so crossing to the ask costs about 1.00¢.

Re-run paying that, and then paying considerably more:

| execution assumption | legs | markets | net/share | t |
|---|---|---|---|---|
| mid (original) | 2,961 | 412 | +9.47¢ | 8.49 |
| **ask (+1.00¢)** | 2,972 | 412 | **+8.44¢** | **7.57** |
| +2¢ (twice the half-spread) | 2,976 | 412 | +7.45¢ | 6.71 |
| +3¢ (three times) | 2,976 | 412 | +6.45¢ | 5.80 |

The threshold scan holds at the ask too: +8.44¢ at 10 points, +13.62¢ at 15,
+21.78¢ at 20.

This is now the only claim in the file that has passed every check applied to
it — per-market aggregation, all three assets, all three time bands, all three
date ranges, the staleness test that killed six other findings, and an execution
cost of three times the measured spread.

### The limitation that remains

It covers **six days**. The model's own accuracy was verified stable over
thirty, but *model versus book* has only been tested on Sep 5–11. A 22-day
extension is running. Until it returns, this is a strong result on a short
window, which is precisely the shape of several findings retracted earlier in
this file.

---

## 37. Controls: one failed because its data is bad, one passed and found a bias

### The 5-minute negative control is not usable

Running the identical pipeline on 5-minute markets produced a *larger* edge than
the hourly one — +15.60¢ at t = 16.76 — while the model's log-loss there was
**worse** than the book's (0.4588 against 0.4540). Those two cannot both be
true, so something was wrong.

Auditing `prices-history` against the recorded book, the same two-source method
as before:

| market | median p − mid | **mean \|p − mid\|** |
|---|---|---|
| hourly | +0.00¢ | **1.15¢** |
| 5-minute | +0.00¢ | **8.70¢** |

Centred in both, but the two independent sources disagree by nearly **nine
cents** on 5-minute markets against one on hourly — which is exactly the
magnitude needed to manufacture the edge that appeared. The control is
inconclusive: **its inputs are unreliable**, so it neither confirms nor refutes
the hourly result. The hourly measurement, where the sources agree to 1.15¢,
stands.

### The placebo test passed, and quantified a bias worth subtracting

Shuffling market-level outcomes and re-running everything, twenty times:

| | net/share | t |
|---|---|---|
| real | **+11.36¢** | **+18.54** |
| placebo, 20 runs | +2.91¢ mean, [+1.21, +4.36] | +3.16 mean, [+1.30, **+4.81**] |

The real statistic exceeds every placebo by a factor of four, so the pipeline is
not generating the result from nothing.

But **the placebo is +2.91¢, not zero**, and that has to be explained rather
than ignored. The selection `|model − mkt| ≥ 0.10` preferentially picks the
*cheaper* side — mean ask 0.440. With outcomes shuffled the win rate returns to
roughly 50%, so `0.50 − 0.44 − fee ≈ +4¢` falls out of the selection alone. It
is a selection bias, not alpha.

**Edge above the placebo baseline: 11.36 − 2.91 = +8.45¢ a share.**

### Where the hourly claim now stands

| check | result |
|---|---|
| markets | 1,581 (22 days) |
| per-market aggregation | t = 18.54 |
| paying the ask, not the mid | survives (+8.44¢ on the 6-day cut) |
| three times the measured spread | survives (+6.45¢, t = 5.80) |
| staleness | survives; disagreement *falls* with staleness |
| source agreement | 1.15¢ between two independent feeds |
| null strategies (random, always-Up, always-cheap, always-dear) | all correctly negative |
| placebo (shuffled outcomes) | t 18.54 against a placebo max of 4.81 |
| **net of the placebo bias** | **+8.45¢/share** |

What has *not* been tested is a fill: every number here assumes the quoted price
is transactable for the size traded, and no order has ever been sent.

---

## 38. A live arm crosses t = 2 — and why that is not yet a result

| arm | markets | mean/market | sd | t | markets for t=2 |
|---|---|---|---|---|---|
| **`spot`** (no model, spot vs strike) | 62 | **+14.26%** | 47.8% | **+2.35** | 45 |
| `fav` (ask ≥ 0.50) | 46 | +9.05% | 31.6% | +1.94 | 49 |
| `filt` (ask ≥ 0.50 + VRP) | 43 | +4.77% | 40.1% | +0.78 | 282 |

`--report` totals: `spot` +14.04% on $736 of stake, `fav` +7.69%, `filt` +6.16%.
All three survivors are positive.

**Six arms have been watched this session.** Bonferroni puts the threshold at
α = 0.05/6, i.e. **|t| > 2.64**, and 2.35 does not reach it. This is the same
trap as section 29, where two of twelve subgroups cleared a nominal t = 2 and
neither survived correction. Reporting `spot` as significant because it is the
one that crossed would be selecting on the outcome.

### An unresolved contradiction worth stating

The `spot` rule was measured **offline** in section 19, on live quotes only and
per observation: **+0.68¢ a share, t = 0.27 — no edge**. The live arm running the
same rule reports +14.26% per market at t = 2.35.

They are not the same measurement — the live arm adds the staleness guard,
sequential hedging, Kelly sizing and a drawdown limit, and it aggregates by
market rather than by observation. But the gap is large enough that **at most
one of them describes reality, and it is not yet clear which.** Recording it
rather than picking the flattering one.

### Circuit breaker aftermath

`base` and `vrp` halted earlier at 31.0% and 27.6% drawdown and never reopen —
the halt has no reset path. Both had zero committed capital and no fills for
over half an hour, so they were stopped. Their data is kept; they are the two
arms with the most negative t, and the breaker took exactly them while leaving
`spot` (+2.35) and `fav` (+1.94) untouched.

**Sample still insufficient under correction. No parameters changed.** The
hourly arm's first markets settle shortly.

---

## 39. The contradiction resolves: both measurements were right about different things

> **RETRACTED within the hour — see section 40.** The numbers below come
> from a decomposition that did not conserve money: it priced hedged pairs
> from the FIFO ledger and the naked residue at blended average cost, and was
> short $46 of spend on a $797 book. The residue was not +$5.32; it was
> −$45.77. The structural claim reverses with it.

Section 38 recorded a gap it could not explain — the spot rule measured offline
gave +0.68¢ at t = 0.27 (no edge) while the live arm running it reports +14.26%
per market. `--report` splits the arm's P&L and the answer is immediate:

```
paper100_spot, 63 markets
  hedged pairs    609.0 sh   cost $513.24  payout $608.97   P&L  +$95.73
                  cost per $1 pair = $0.8428
  naked residue    47 legs   cost $238.03  payout $243.35   P&L   +$5.32   win 22/47
  TOTAL                                                            +$101.05  (+13.45%)
```

**Ninety-five percent of the profit is the hedged pairs. The naked residue — the
part that *is* the pure directional bet — won 47% and made $5.32**, which is the
offline finding exactly. Both measurements were correct; the offline test scored
direction and the arm earns from hedging. They were never measuring the same
thing, and section 38 compared them as if they were.

The same split holds in the other two arms:

| arm | hedged pairs | naked residue | naked win rate |
|---|---|---|---|
| `spot` | **+$95.73** | +$5.32 | 22/47 (47%) |
| `fav` | **+$68.29** | −$26.92 | 15/30 (50%) |
| `filt` | **+$53.72** | −$24.00 | 18/31 (58%) |

Pairs profitable in all three, directional residue around zero or negative in
all three.

### What still has to hold for this to be real

Section 5 established that the sequential lock is **not** arbitrage — it is the
winning branch of a directional bet, with the losing branch appearing as naked
residue. It pays here only because the residue is roughly break-even (+$5.32)
rather than deeply negative. If that balance is a property of 63 markets rather
than of the instrument, the pairs' $0.8428 per dollar goes with it.

That is the thing to watch, and it is not yet answerable: `spot` has 63 markets
against the ~45 its own variance demands for t = 2, but under the six-arm
Bonferroni threshold of |t| > 2.64 it remains unproven at 2.35.

---

## 40. The decomposition was losing $46, and fixing it reverses section 39

Section 39 claimed the profit lived in hedged pairs while the naked residue
broke even. Before writing anything further I checked whether the split adds
back up. It does not:

```
paper100_spot   pairs + residue cost   513.24 + 238.03  =  751.27
                opens + hedges  cost   638.99 + 158.54  =  797.53
                                              missing  =   -46.26
```

Every share settles at exactly 0 or 1, so regrouping the same legs into
pairs-and-residue **must** reproduce open-legs-plus-hedge-legs. The gap was
a costing-basis mismatch: pairs came from the FIFO `matched` ledger while the
residue was priced at blended average cost. The lots FIFO consumed are not the
average, so the survivors were valued too cheaply — the residue looked $46
better than it was. This is the same average-cost leak already fixed once for
the pairs, still live on the other side of the split.

The residue is now taken by subtraction (`total spend − matched cost`), which
conserves by construction, and `report()` prints an identity check that fails
loudly if it ever stops holding.

### What the arms actually show

| arm | pairs | residue | **NET** | was |
|---|---|---|---|---|
| `paper100_spot` | +$105.20 | **−$45.77** | **+$59.43 (+6.87%)** | +13.45% |
| `paper100_fav` | +$69.17 | **−$56.45** | **+$12.71 (+2.14%)** | +7.61% |
| `paper100_filt` | +$54.67 | **−$36.12** | **+$18.54 (+3.65%)** | +6.09% |
| `paper_lock` | +$2,215.42 | **−$2,234.36** | **−$18.94 (−0.07%)** | — |
| `paper_dir` | +$2,768.60 | **−$3,247.65** | **−$479.06 (−2.17%)** | — |

The structure reverses: pairs profitable and residue **deeply negative in every
single arm**, not break-even. `paper_lock` is the cleanest statement of it —
$2,215 of "locked profit" against $2,234 of residue loss, netting −$19 on a
$28,432 book.

### The pair cost was never a result

"Cost per $1 pair = $0.84" is not a measurement of profit. You only ever get a
cheap pair when the first leg already moved your way; when it moves against
you the hedge is expensive, you decline it, and the leg dies naked. **The
number is conditioned on having been right**, and the branch it conditions away
is precisely the residue. Reporting the pair cost without it is selection
effect quoted as edge, and this project has quoted it for many sections.

### What is left is two questions, not three

With the identity restored, the arithmetic is exact in all five arms:

```
NET  =  (open legs held to settlement)  +  (hedge legs held to settlement)

spot   +59.43  =   +44.44  +  +14.99
fav    +12.71  =    -6.83  +  +19.54
filt   +18.54  =   -24.27  +  +42.81
lock   -18.94  =  +411.97  + -430.90
dir   -479.06  = -1466.19  + +987.13
```

Pairing is pure regrouping and contributes nothing of its own. The only two
questions are whether the open legs have directional edge and whether the hedge
legs do — and "hedging changed the result by $X" is now visibly just the hedge
leg's own directional P&L under another name.

### Neither has been shown

| arm | hedge leg alone | per market | t |
|---|---|---|---|
| `paper_dir` | +21.10% | +$7.65 | +1.95 |
| `paper100_filt` | +49.93% | +$1.53 | +1.69 |
| `paper100_fav` | +20.92% | +$0.51 | +0.67 |
| `paper100_spot` | +9.07% | +$0.30 | +0.47 |
| `paper_lock` | −3.66% | −$2.58 | −0.78 |

Not one reaches |t| = 2, let alone the 2.64 the six-arm Bonferroni demands —
and the two arms with the largest samples (183 markets each) sit at −0.07% and
−2.17% net. Section 39's "one live arm crosses t = 2" does not survive either:
it was measuring the inflated total.

---

## 41. Predicted edge does not predict realised P&L

Section 40 collapsed the strategy to two directional bets, which leaves exactly
one thing that can make money: the model's edge at entry being real. That is now
measured directly. Every traded leg is bucketed by its predicted edge per share
and compared against what the share actually paid, with t clustered at market
level because legs inside one market share an outcome.

```
paper_dir                          paper_lock
pred edge  legs   pred    real  t     legs   pred    real  t
0-2c       1004  0.0133 -0.0323 -1.63 1064  0.0137 -0.0129 -0.73
2-4c        689  0.0287 -0.0322 -1.34  877  0.0290 +0.0113 +0.55
4-6c        233  0.0498 -0.0562 -2.37  356  0.0491 +0.0059 +0.17
6-8c        123  0.0691 +0.0534 +0.83  176  0.0697 +0.0116 +0.19
8-10c        95  0.0908 -0.0733 -1.06   96  0.0894 +0.0153 +0.22
10-12c       51  0.1108 +0.0360 +0.40   73  0.1079 -0.0199 -0.31
12c+        413  0.2653 +0.1139 +2.36  250  0.2204 -0.0093 -0.19
```

No monotone relationship anywhere. The two largest arms disagree in sign in
five of seven buckets. `paper_dir`'s 12c+ cell is the one result that looks
like something — 413 legs, predicted 26.5¢, realised +11.4¢, t = 2.36 — and
`paper_lock`, the independent replication on the same 183 markets, puts the
same bucket at **−0.9¢**. It does not replicate.

Across five arms × seven buckets, 35 cells produce three at |t| > 2. Chance
produces 1.6. This is noise with the shape of a result.

### Everything now points the same way

| measurement | result |
|---|---|
| offline spot rule, live quotes (§19) | +0.68¢, t = 0.27 |
| open legs held to settlement (§40) | +6.35%, −1.37%, −5.74%, +2.47%, −8.42% |
| hedge legs held to settlement (§40) | no arm reaches \|t\| = 2 |
| predicted edge vs realised (this section) | flat, non-monotone, non-replicating |

The book prices these contracts about as well as the model does. **The model's
residual edge over the book is nil**, and that is not a filtering problem —
no filter recovers signal from a quantity that carries none.

### What that leaves

The taker fee is `7% × (1-p)` of stake — **3.5% at the money, per trade**. Against
a measured edge indistinguishable from zero, the fee is the entire story. The
only remaining way to be profitable is to stop paying it: post rather than take.
That was closed on 5-minute markets for a mechanical reason — the measured queue
takes 6.6 minutes to clear in a 5-minute market — but that argument does not
carry to the hourly markets, where the same queue sits inside a 60-minute window
against 10× the liquidity. That is the next thing to measure, and it is the last
structurally different idea this project has left.

---

## 42. The book is calibrated, so taking loses at every price

Section 41 showed the model's edge over the book is nil. That invites the
obvious follow-up: is the *book* beatable by anyone taking, model or not? The
traded legs cannot answer it — they were selected on the model's opinion, so a
gradient across them confounds the market with the filter that picked them. The
`sample` rows can: they are the book quoted on a fixed schedule regardless of
what the model thought.

5,373 quotes across 168 settled markets, bucketed by the ask, against what
actually happened:

```
ask          n    mkts  mean ask   won   net/sh after fee    t
0.0-0.1     768   168     0.028   0.017     -0.0129       -0.96
0.1-0.2     321   134     0.145   0.069     -0.0847       -3.31
0.2-0.3     398   153     0.245   0.246     -0.0113       -1.53
0.3-0.4     465   147     0.347   0.340     -0.0235       -1.43
0.4-0.5     589   161     0.450   0.382     -0.0851       -4.29
0.5-0.6     968   166     0.529   0.551     +0.0043       +1.19
0.6-0.7     504   154     0.644   0.633     -0.0268       +0.43
0.7-0.8     424   157     0.747   0.743     -0.0170       +0.93
0.8-0.9     377   148     0.839   0.836     -0.0124       -2.12
0.9-1.0     462   157     0.955   0.961     +0.0034       +0.26
```

Read the `mean ask` and `won` columns side by side: 0.245 → 0.246, 0.347 →
0.340, 0.644 → 0.633, 0.747 → 0.743, 0.839 → 0.836, 0.955 → 0.961. **The book
is calibrated.** It is pricing these contracts correctly.

After the `7% × (1-p)` taker fee, net per share is negative in eight of ten
buckets and positive in none at any meaningful t. The best cell in the table is
+0.43¢ at t = 1.19.

**This closes the taking case for any taker, not just for this model.** No
filter, no regime gate, no sizing rule and no options analogy changes it,
because none of them change the two facts it rests on: the book is right, and
the fee is positive.

### One genuine anomaly, and why it is not tradable as measured

The 0.1-0.2 bucket asks 14.5¢ for something that happens 6.9% of the time,
t = -3.31 — textbook favourite-longshot bias. That is a **short**, and this
harness only ever buys. Buying the complement is the natural expression, but
the 0.8-0.9 bucket prices its side correctly (0.839 vs 0.836), so the two do
not line up into a trade. They are also not paired observations: a sample row
is written per (market, side) when the strategy evaluates it, and the two sides
are not logged at the same instants, so the bucket pair cannot be read as one
market's two legs. Treat it as unexplained, not as an edge.

### What is left

Taking is dead on arithmetic. The fee is `7% × (1-p)` of stake — 3.5% at the
money — against a book that prices correctly, so the only remaining structure
is to collect that fee rather than pay it. `makercheck.py` is measuring the
markout on resting bids in the hourly markets now. If posted orders are
adversely selected by more than the rebate is worth, this instrument has
nothing in it and that is the finding.

---

## 43. The favourite beats its own price — measured three times, wrong twice

The calibration table in section 42 has one structure in it worth chasing: the
0.4-0.5 and 0.5-0.6 cells miss in **opposite** directions. That is not two
findings, it is one — the side priced above the money wins more often than its
price implies. A base-rate skew cannot produce it, because that would move both
sides of a market the same way and the buckets pool both sides.

Getting a number out of it took three attempts, and the first two were wrong in
ways worth keeping.

**Attempt 1 — clustered on the market.** Favourite defined as `ask > 0.5`,
samples in 0.35-0.65, clustered per market: `+0.0905, t = +4.00` at tau > 90s.
But btc, eth and sol resolve the same five minutes of the same risk asset.
Three markets per window is one observation wearing three hats. Clustering on
the **window** instead: `t = +2.48`. The sqrt(3) was exactly the correlation I
had not priced.

**Attempt 2 — "ask > 0.5" does not name a side.** The two asks sum to about
$1.035, so near the money *both* sit above 0.50 and the same instant is counted
as two favourites. Raising the floor to 0.54 forces the complement under 0.50
and makes the rule expressible — and the effect collapsed to `t = +1.22` and
`+0.39`. But that floor also discards the genuine mild favourites, so it is not
the honest version either. Both attempts were measuring partly-undefined
objects.

**Attempt 3 — let the complement name the side.** The `pairs` table quotes both
sides at the same instant, so the favourite is simply the dearer one. No
threshold, no ambiguity, 207k paired quotes:

```
tau        quotes  windows   too timid   net/sh after fee      t
< 30s       1,666       13     +0.0654        +0.0485       +0.62
30-90s      6,547       45     +0.0739        +0.0571       +1.75
> 90s      62,606       64     +0.0504        +0.0335       +1.87
```

Consistent in sign across **all three** tau cells now, where attempt 1 flipped
sign in the middle cell. It also does not concentrate near expiry, which is
what a stale book would look like. `paper_lock` reproduces it at +0.62 / +1.62
/ +1.68 on the same windows — the same measurement, not a replication.

### This is not a result yet

t ≈ 1.8 on 64 windows. The effect is +3.4¢ per share after fee on a ~57¢ stake,
about +5.9% a trade, which is large enough to be suspicious on its own. Getting
to the Bonferroni threshold at this effect size needs roughly 138 windows —
about 11.5 hours of 5-minute markets, and the collectors are still running.

Note what it would mean if it holds: it is a **taking** edge, on the same
markets section 42 declared closed. Both can be true — 42 says the book is
calibrated *on average across all prices*, and this says it is not calibrated
*conditional on being the dearer side near the money*. The average of a
correctly-signed bias and its mirror is zero.

### Forward test

Two arms, `--fav-only 1` and `--fav-only -1`, buying the dearer and the cheaper
side respectively at flat $3 stakes with hedging disabled, $100 each. They are
complements of the same quotes, so **they cannot both be profitable** — their
sum is exactly minus twice the fee. That makes the pair its own instrument
check: if both show a profit, the harness is broken, and I would rather find
that out from a rule that cannot be true than from a number I want to believe.

---

## 44. Five-minute crypto has real momentum — measured with no volatility estimate

> **Band labels corrected in section 49.** The `by_price` table's buckets
> are 10¢ wide, not 5¢; every row label was off. The sigma-free persistence
> result is unaffected.

> **Partly retracted — see section 45.** The momentum result stands; the
> claim that it "cross-validates" section 43 does not. The 30-day test scores
> the book against the *random walk*, section 43 scored it against the *book's
> ask*. Those are different quantities and their numerical agreement was a
> coincidence I read as confirmation.

Section 43's favourite effect had an obvious alternative explanation: "the
favourite" is just "the side currently ahead", and five trending hours would
manufacture it. 64 windows cannot separate those. 30 days of 1-minute klines
can, and no book data is involved, so nothing in it can be a book artifact.

Pricing the lead with a driftless random walk against 8,627 windows per asset:

```
tau           obs  windows   mean p   too timid       t
60s        25,881    8,627    0.836     +0.0261  +11.47
120s       25,881    8,627    0.768     +0.0237   +8.24
180-240s   51,762    8,627    0.675     +0.0134   +4.48
```

And bucketed by the favourite's price, net of the `7%p(1-p)` fee — the table
that decides tradability, because the same edge in probability points is worth
very different amounts at different prices:

```
price          obs  windows   too timid    net/sh   t(net)
0.50-0.55   26,907    7,139     +0.0314   +0.0141    +3.60
0.55-0.60   21,159    7,495     +0.0528   +0.0369    +8.91
0.60-0.65   17,184    7,275     +0.0410   +0.0279    +6.86
0.65-0.70   14,844    6,765     +0.0220   +0.0130    +3.63
0.70+       23,430    6,396     -0.0124   -0.0150    -6.22
```

A hump on 0.55-0.65 that turns **negative** above 0.70. The live measurement in
section 43 used real book prices in exactly the ≤0.65 band and gave +3.35¢ per
share; this gives +2.79¢ to +3.69¢ in the same band from entirely different
data with an entirely different price source.

### The estimator was doing some of the work

Sweeping the trailing-vol window moves the answer:

```
LOOK= 10 bars  net/sh +0.0299  t +6.98
LOOK= 20 bars  net/sh +0.0345  t +8.24
LOOK= 60 bars  net/sh +0.0374  t +9.09
LOOK=120 bars  net/sh +0.0392  t +9.61
LOOK=240 bars  net/sh +0.0440  t +10.89
```

Monotone in the length of the lookback, which is the signature of a sigma that
runs high: too much assumed volatility puts the model price too near 0.5 and the
favourite beats it for no reason but the estimator. A result that moves with a
free parameter is not yet a property of the market.

### So take sigma out entirely

For Brownian motion the chance that the sign of the move survives to the end of
the window depends only on the fraction elapsed, through
`corr(W_t, W_T) = sqrt(t/T)`:

```
P(W_T > 0 | W_t > 0) = 1/2 + arcsin(sqrt(t/T)) / pi
```

No volatility, no estimation, no fitted parameter. Against 30 days:

```
elapsed   windows   brownian   actual    excess       t
1/5         8,590     0.6476   0.6607   +0.0131   +3.30
2/5         8,613     0.7180   0.7358   +0.0178   +4.95
3/5         8,620     0.7820   0.8037   +0.0216   +6.80
4/5         8,621     0.8524   0.8758   +0.0234   +9.28
```

**Five-minute crypto windows persist more than a martingale allows**, by 1.3 to
2.3 percentage points, growing with elapsed time, on 8,600 independent windows
with nothing fitted. That is a property of the price path.

### But this is a fact about the model, not yet about the market

Momentum in the series is only worth money if the **book** misses it, and
section 42 measured the book as calibrated at exactly the prices where this
effect is largest: ask 0.839 → won 0.836, ask 0.955 → won 0.961. If the book
already prices the persistence, the 30-day test measures the random walk's
error and not an inefficiency — which is precisely what sections 41 and 42
concluded from the other direction.

Section 43's +5pp on the book's own dearer side says the opposite, at t = 1.8 on
64 windows. The two cannot both be right and the live sample is far too small to
settle it. The next measurement has to compare the outcome against the **book's**
price, split by mid and by ask: if the book's mid is calibrated but its ask is
not beatable, the entire edge sits inside the spread, and that makes this a
maker question rather than a taker one.

---

## 45. The book prices the momentum. There is no taker edge.

> **Refined by section 49.** Same labelling bug. "No taker edge" stands
> (pooled t = 0.34). "The book prices the momentum" is too strong: corrected,
> the 5-minute book still leans +2.4pp and the hourly book +4.3pp at t = 3.89.
> The book carries *most* of it; the fee kills what is left.

Section 44 ended with the right question: momentum in the price path is only
worth money if the **book** misses it. `pairs` quotes both sides at one instant,
so the book's belief about Up is the mid, `(ask_up + 1 - ask_dn)/2`, and what a
taker pays is the ask. Scoring the outcome against each separates "the book is
wrong" from "the book is wrong by less than the spread".

```
mid            quotes windows   vs mid      t  half-spd  taker net      t
0.50-0.55      52,358      66  +0.0601  +2.32   +0.0075    +0.0354  +1.36
0.55-0.60      37,057      61  +0.0239  +0.78   +0.0070    +0.0012  +0.04
0.60-0.65      28,596      60  +0.0086  +0.25   +0.0070    -0.0112  -0.33
0.65-0.70      25,546      61  +0.0242  +0.83   +0.0076    +0.0081  +0.28
0.70+          65,291      61  +0.0067  +0.77   +0.0058    -0.0003  -0.04
```

Compare the `vs mid` column against what section 44 predicted from 30 days of
klines for the same bands: +3.14, +5.28, +4.10, +2.20, −1.24 points. The book
delivers +6.01, +2.39, +0.86, +2.42, +0.67, and only the first has any t at all.
**Where the 30-day data says the momentum is largest — 0.55 to 0.65 — the book
is right to within a point.** The random walk misses the persistence; the book
does not.

And the spread is not the obstacle: the half-spread is 0.7¢ while the fee at
these prices is about 1.6¢. The fee is more than twice the spread.

### Section 43 was 66 windows of noise

The one cell with a t is 0.50-0.55 at +2.32 — and that is the band the 30-day
data ranks **weakest** of the positive buckets (+3.14 points, the smallest).
Section 43's headline pooled quotes ≤0.65, where this bucket supplies 52k of
118k, so §43's result *is* this cell. One bucket out of five, 66 windows, t =
2.3, pointing the wrong way relative to 8,600 windows of history. That is what
a false positive looks like, and I built two live arms on it before checking.

The forward-test arms stay up. They cost nothing, and a rule I now expect to
fail is a better test of the harness than one I expect to pass — `--fav-only 1`
and `-1` are complements and cannot both profit, so they audit the ledger
regardless of what they say about the market.

### Where this leaves the whole project

| question | answer |
|---|---|
| does the model beat the book? | no (§41) |
| is the book calibrated? | yes (§42) |
| does hedging add anything? | no, it is a second directional bet (§40) |
| does 5-min crypto have momentum? | yes, genuinely (§44) |
| does the book price that momentum? | yes (this section) |
| is there any taker edge? | **no** |

Taking is closed on arithmetic and now on mechanism too. The fee is 1.6¢ where
the spread is 0.7¢, so the only remaining structure in this instrument is to be
on the receiving side of both. `makercheck.py` has been measuring markout on
resting hourly bids for the last half hour, and it decides whether anything is
left here at all.

---

## 46. The hourly market is a different instrument for a maker — by a factor of ten

A maker's gross edge here is the half-spread and nothing else: there is no
reliable exit, because crossing back costs the full spread twice over, so every
fill is a directional position held to settlement. The edge survives only if the
mid moves less than the half-spread while the order rests. That ratio is
measurable from the paired quotes, and it is not close to the same on the two
instruments.

```
                      half-spread    mid travel in 10s   ratio
5-minute markets          0.50c           2.50c          0.2  (5x against)
hourly markets            1.00c           0.50c          2.0  (2x in favour)
```

```
5-minute   mid travel   10s median 2.50c   p75  7.00c   p90 13.50c  (n=207k)
                        30s median 6.00c   p75 14.50c   p90 26.00c
                        60s median 11.00c  p75 23.50c   p90 38.00c
hourly     mid travel   10s median 0.50c   p75  1.50c   p90  3.00c  (n=46k)
                        30s median 1.00c   p75  2.50c   p90  5.00c
                        60s median 2.00c   p75  4.00c   p90  7.00c
```

The hourly book is **five times calmer and quotes twice as wide**. A ten-fold
swing in the only ratio that matters to a maker.

That is the first structural fact this project has found that favours doing
something rather than not doing it, and it explains why the 5-minute markets
were hopeless from both sides at once: a taker pays a 1.6¢ fee into a calibrated
book, and a maker offers 0.5¢ of edge against a mid that moves 2.5¢ in ten
seconds.

### It is still thin, and adverse selection is the whole question

A 1¢ edge against 0.5¢ of ten-second travel is a margin, not a moat, and the
travel figure understates the danger: fills are not random draws from that
distribution. A resting bid is hit precisely when someone with a Binance feed
has seen the price move — and on a BTC market, that is everyone. The relevant
number is not how far the mid travels, it is how far it travels **after your
order fills**, which is markout, and `makercheck.py` has been measuring exactly
that on live hourly books. If markout is worse than −1¢ there is nothing here.

Worth stating plainly what the prize is, since it is larger than the spread
alone: a maker avoids the `7% × (1-p)` taker fee as well, about 1.7¢ at the
money. Spread plus fee is ~2.7¢ a share against a ~55¢ contract, close to 5%
per position. That is the number adverse selection has to eat before this is
dead.

---

## 47. Two small corrections that both go the same way

**The five-bucket sign pattern was not evidence.** Section 45's `vs mid` column
is positive in all five price bands, which reads like a 1-in-32 sign test. It is
not: the five buckets are five looks at the same 67 windows, so they cannot
multiply. Pooled to one number per window and tested once:

```
pooled vs mid        67 windows   +0.0288   t=+1.38
pooled taker net     67 windows   +0.0118   t=+0.57
```

The book's mid may lean about 2.9 points, and nothing survives the fee. Section
45's conclusion is unchanged; the apparent extra confirmation was an artifact of
counting correlated tests as independent ones — the same error as clustering
btc/eth/sol as three markets in section 43.

**Momentum decays with horizon.** The hourly markets settle on the same
close-vs-open shape twelve times longer, so the sigma-free persistence test runs
on them unchanged. Pooled into quarters of the window:

```
elapsed      5-minute            hourly
             excess      t       excess      t
0-25%       +0.0131   +3.30     +0.0083   +0.76
25-50%      +0.0178   +4.95     -0.0055   -0.48
50-75%      +0.0216   +6.80     +0.0106   +1.15
75-100%     +0.0234   +9.28     +0.0144   +2.57
```

Strong and monotone at five minutes, weak and mostly absent at an hour, with
only the final quarter reaching t = 2.57 against a four-test threshold of 2.50.
Microstructure momentum decaying with horizon is a standard result and this is
a clean instance of it on 715 hourly windows.

The first run of this printed one row per offset — 59 rows on 715 windows — and
two of them showed t > 2.7. Sixty tests produce that by construction. The table
was rebuilt to pool before it could be read.

### What it means for the maker case

The instrument that favours making (hourly, §46) is the one with **no
predictability left in it**. That is not a disappointment: market making does
not need a forecast, it needs the mid to sit still while the order rests, and
that is exactly the axis on which hourly beats 5-minute by ten to one. It does
mean there is no directional overlay to stack on top — the spread is the whole
thesis, and adverse selection is the only thing that can take it away.

---

## 48. The hourly book's mid is biased, the fee eats it, and a maker would keep it

> **RETRACTED — see section 51.** The bias is an equal-weighting artifact.
> Weighted the way a trader is actually exposed, it is +0.39pp, not +4.30pp.

> **Numbers superseded by section 49** — same labelling bug, and the
> "pre-specified 0.55-0.65 band" actually pooled 0.60-0.80. Corrected, every
> figure in this section gets *stronger*, not weaker.

`histtest22.json` holds 93,016 quotes across 1,581 hourly markets over 22 days —
**527 independent hours**, against the 67 windows every live conclusion so far
has rested on. Section 44 predicted, from 30 days of klines and before this file
was scored, that persistence produces a hump over 0.55-0.65 that turns negative
above 0.70. That band is therefore pre-specified, not chosen after the fact.

First, what the price series actually is. `prices-history` returns a **mid**,
not an ask — verified by pulling both tokens of one market and summing them at
paired timestamps: median exactly 1.0000, where two asks would sum to about
1.02. A taker does not get this price. They cross the half-spread (1.0¢ on
hourly, §46) *and* pay the fee. A maker is handed the half-spread and pays no
fee. Costing it correctly is the difference between a result and a mistake:

```
price             obs  hours   vs mid      t    taker      t    maker      t
0.50-0.55      26,263    526  +0.0317  +3.67  +0.0045  +0.52  +0.0417  +4.83
0.55-0.60      17,452    515  +0.0454  +3.70  +0.0197  +1.61  +0.0554  +4.52
0.60-0.65      12,884    514  +0.0263  +2.17  +0.0036  +0.29  +0.0363  +2.99
0.65-0.70      12,425    513  +0.0125  +1.16  -0.0060  -0.56  +0.0225  +2.09
0.70+          23,992    512  -0.0042  -0.68  -0.0162  -2.61  +0.0058  +0.93
```

One pre-specified test on the predicted band, 517 hours:

```
0.55-0.65 pooled
  vs mid   +0.0339   t = +2.93     the book's mid IS biased
  taker    +0.0095   t = +0.82     the fee and spread eat all of it
  maker    +0.0439   t = +3.79     a maker keeps 4.4c a share
```

Three things at once, and they are consistent with everything before them:

1. **The hourly book under-prices the favourite by 3.4 points** at 0.55-0.65 —
   the persistence section 44 found in the price path, which the book does not
   fully carry. Predicted band, independent data, t = 2.93 on 527 hours.
2. **A taker gets nothing**, t = 0.82. Sections 41, 42 and 45 said taking is
   dead and this says it again on 8× the sample: the edge exists and the
   `7% × (1-p)` fee plus the spread is larger than it.
3. **A maker would keep +4.4¢ a share**, about 7.7% of a 57¢ position, t = 3.79.

The 0.70+ row is the other half of the same story: as a taker it is
*significantly negative*, t = −2.61, exactly where section 44 said persistence
reverses. A mechanism that predicts its own failure region is a better sign than
one that only predicts wins.

### The assumption this entirely rests on

The maker column assumes fills arrive independent of what happens next. That is
the one thing it cannot assume: a resting bid on the favourite is hit precisely
by someone selling it, and on a BTC market anybody with a Binance feed knows
which way it just moved. **The maker number is an upper bound**, and adverse
selection can only take it down.

The size of the question: 4.4¢ would have to be erased by post-fill drift, and
the hourly mid's median travel is 0.5¢ in ten seconds and 2.0¢ in sixty (§46).
Erasing 4.4¢ needs fills concentrated near the p90 of a full minute's movement.
Not impossible — that is what informed flow does — but it is a large ask, and
`makercheck.py` has been measuring the actual markout on live hourly books for
the last hour rather than arguing about it.

---

## 49. A bucket-width bug, and the corrected result is stronger

> **Every figure in this section is hour-clustered — see section 65 for the
> exposure-weighted versions. The taker column changes sign.**

> **Maker column retracted — see section 51.** The bucket fix was correct
> and the klines figures stand. The hourly *book* bias does not.

`int((p - 0.5) * 10)` makes buckets **10¢ wide**. I labelled them 5¢ wide, in
three files, so every band in sections 44, 45 and 48 named the wrong prices: the
row printed as "0.55-0.60" held 0.60-0.70, and section 48's "pre-specified
0.55-0.65 band" actually pooled 0.60-0.80. The arithmetic was right throughout;
the labels were not, which is worse than a wrong number because it reads as
confirmation of a prediction it never tested.

Fixed to integer cents — `int(round((p - 0.5) * 100)) // 5` — because prices sit
on a 1¢ grid and `(0.60 - 0.5) * 20` evaluates to 1.9999999999999996, which put
exactly 0.60 one bucket low.

### All three datasets now agree, and they are independent

`too timid` — how far the favourite's win rate exceeds its own price:

```
price        30d klines (5m)   22d hourly book   live 5m book
             8.5k windows       527 hours         68 windows
0.50-0.55      +0.0060           +0.0188           +0.0374
0.55-0.60      +0.0450           +0.0377           +0.0550
0.60-0.65      +0.0491           +0.0492           +0.0337
0.65-0.70      +0.0457           +0.0465           +0.0292
0.70+          +0.0071           -0.0114           +0.0004
```

A plateau of roughly +4.5 points across 0.55-0.70, flat outside it, in three
samples that share no data: 1-minute candles with no book at all, 22 days of
hourly book quotes, and 5 hours of live 5-minute quotes. The klines and the
hourly book match to within 1.5 points in every band.

### The corrected hourly result

```
price             obs  hours   vs mid      t    taker      t    maker      t
0.50-0.55      13,485    525  +0.0188  +2.19  -0.0086  -1.01  +0.0288  +3.36
0.55-0.60      12,090    515  +0.0377  +3.43  +0.0107  +0.97  +0.0477  +4.34
0.60-0.65       7,906    512  +0.0492  +3.80  +0.0229  +1.77  +0.0592  +4.57
0.65-0.70       9,196    511  +0.0465  +3.58  +0.0212  +1.63  +0.0565  +4.34
0.70+          50,339    518  -0.0114  -1.33  -0.0283  -3.30  -0.0014  -0.16

0.55-0.65 pooled, 517 hours:
  vs mid   +0.0430   t = +3.89
  taker    +0.0163   t = +1.47
  maker    +0.0530   t = +4.79
```

Every figure moved up: the bias is +4.30 points rather than +3.39, and the maker
number is +5.30¢ a share at t = 4.79 rather than +4.39¢ at t = 3.79.

### It is stable where it should be

```
by asset (0.55-0.65)      hours   vs mid      t    maker      t
  btc                       512  +0.0513   +3.61  +0.0613  +4.32
  eth                       510  +0.0534   +3.81  +0.0634  +4.52
  sol                       513  +0.0725   +5.33  +0.0825  +6.07

by day: 21 of 23 days positive
```

Three assets independently, and 21 of 23 days. Not one or two days carrying a
t-statistic.

### What stands

- Taking is still dead: +1.63¢ at t = 1.47 in the best band, and
  **significantly negative** at 0.70+ (t = −3.30).
- The book carries most of the persistence but not all of it — section 45's
  "the book prices the momentum" was too strong.
- A maker in the 0.55-0.65 band would keep +5.30¢ a share, t = 4.79, **if fills
  are independent of what happens next**. That assumption is still the whole
  question and `makercheck.py` is still the thing that answers it.

---

## 50. A maker arm, and the instrument checks it rests on

> **Premise retracted — see section 51.** The arms and the instrument
> checks stand; the +5.3¢ they were built to test does not.

Section 49's number — a maker keeping +5.3¢ a share at t = 4.79 over 517 hours —
assumes fills arrive independently of what happens next. No historical file can
test that, so `run.py` gains `--maker`: it posts at the touch instead of taking,
pays no fee, and fills when the queue ahead of it clears.

It reuses the existing discovery, settlement, equity and drawdown machinery and
writes into the same schema, so P&L still comes only from `--report`. What is
new is `Book.best_bid()`, a `last_trade_price` handler, a resting-order
lifecycle, and a fee override on `log()` — a maker's fee is `0.0`, which is not
the same as `None`, so it is passed rather than derived.

Queue model: a joiner sits behind the size already resting at that price and
fills once cumulative sells at or below it exceed that size. Moving the best bid
cancels and replaces, resetting queue position, which is what happens to a real
order.

Two arms, `maker_fav` on the favourite at mid 0.55-0.65 and `maker_dog` on the
underdog at 0.35-0.45. Complements again, so they cannot both profit.

### Four instrument checks before trusting any of it

**`prices-history` is a mid.** Both tokens of one market summed at paired
timestamps: median exactly 1.0000, range 0.985-1.010. Two asks would sum to
~1.02. This is what forced the spread correction in section 49.

**Trade events carry what the fill model needs.** `last_trade_price` arrives
with `asset_id`, `price`, `size` and `side`. But only **7 trades in 90 seconds
across 12 tokens** — roughly one per token every 2.6 minutes. Worse, the event
may report only price *changes*, so trades at an unchanged price are invisible:
**detected volume is a lower bound, and so is every fill rate built on it.**

**The tick is 1¢ exactly where the strategy lives.** A `tick_size_change` from
0.01 to 0.001 fired during the sample, which would have invalidated the 1¢
half-spread. Checking 108k hourly quotes: 3.1% sit on a sub-cent tick, and they
are **all** at 0.95+. Between 0.50 and 0.70 the figure is 0.0%.

**The pooled estimate is not a boundary artifact:**

```
[0.55,0.65)  516 hours  +0.0449  t=+4.04
[0.55,0.65]  516 hours  +0.0448  t=+4.03
[0.55,0.70)  517 hours  +0.0408  t=+3.65
[0.50,0.70)  527 hours  +0.0384  t=+4.04
```

Split by time remaining, no single ten-minute band is significant on its own
(+0.9 to +3.4 points, t = 0.5 to 2.0) — the edge is spread across the hour
rather than concentrated in it, which is what a maker needs, since a resting
order cannot choose its moment.

### The part that makes this worth running

Of the maker's +5.3¢, only **1.0¢ is spread capture**; +4.3¢ is the book's own
bias. So a maker who has to improve the bid to get queue priority — giving up
the entire spread and buying at the mid — still holds +4.3¢ and pays no fee.
**Queue competition cannot take this edge away; only adverse selection can.**

---

## 51. The book bias was dwell-time selection, and section 45 was right all along

Backtesting the maker rule as an account rather than a t-statistic contradicted
it outright: **t = +0.65**, against +4.79 from the same data and the same band.
A statistic and a backtest disagreeing by that much means one of them is not
measuring what I think.

The backtest was right. Splitting in-band observations by how long the market
stayed in the band:

```
minutes in band   markets      obs   mean excess
1-5                   268      847     +0.2264
6-15                  647    6,798     +0.0522
16-30                 588   12,363     -0.0327
31+                    32    1,128     -0.0530
```

**Dwell time is determined by the outcome.** A market leaves the 0.55-0.65 band
because it resolved — and the ones that left fastest resolved hardest in the
favourite's direction. A market that sits at 0.60 for half an hour is a coin
flip wearing a 0.60 price tag, and its excess is *negative*.

Clustering by hour gives each market one vote regardless of how long it was
tradable. That over-weights the 268 fast-exiting markets — 847 observations —
against the 588 lingering ones carrying 12,363. **A trader cannot weight markets
equally.** A resting order is exposed in proportion to time, so the tradable
estimand is observation-weighted:

```
hour-clustered (each market one vote)   +0.0448   t=+4.03
observation-weighted (exposure)         +0.0039
first in-band quote per market          -0.0019
one random in-band quote per market     +0.0536
```

The number a maker can actually earn is **+0.39 points, which is zero.** The
+4.48 was never available.

### Backtested as an account

```
band 0.55-0.65, $3 flat, 22 days
  1,535 trades over 516 hours (70/day)   win rate 57.8%
  mean $+0.0430/trade   sd $2.608   t=+0.65
  $100 -> $165.99   max drawdown 82.1%
```

+66% in 22 days, and t = 0.65 — the gain is one standard deviation of a random
walk with 1,535 steps. The 82% drawdown on a $100 account settles the practical
question independently of the statistical one.

### What survives, and what this restores

The **price-path momentum stands**: the sigma-free test takes every window at
four fixed offsets with no band conditioning, so dwell-time selection cannot
reach it. And the klines result survives reweighting — observation-weighted it
is still +3.59 points, because the *random walk* genuinely under-prices the
favourite, which the sigma sweep in section 44 already showed is partly the
estimator.

The **book** does not. Observation-weighted, the hourly book's bias is +0.39
points. Section 45 concluded "the book prices the momentum, there is no taker
edge", and section 49 partly retracted that as "too strong". Section 49 was
wrong to. Section 45 was right, and it is now right on 527 hours instead of 67
windows.

### The lesson, which is general

**Any statistic conditioned on a price band inherits selection from how long the
market stayed in that band — and dwell time is a function of the outcome.**
Equal-weighting markets is where it enters. This is the same error as section
40's cheap pairs (conditioning on having been right) and section 43's
"ask > 0.5" (conditioning on an undefined object), in its third costume. Three
times now the trap has been: a quantity that looks like a price is actually a
summary of the path that produced it.

### The maker arms stay up

They are now testing a thesis I expect to fail, which is the more useful
experiment. What is left of the maker case is the half-spread alone — about 1¢
a share, exactly what section 46 said before the book-bias detour — against a
mid that moves 0.5¢ in ten seconds. `makercheck.py` still decides that, and the
fill assumption remains the one thing no file here has ever tested.

---

## 52. The momentum is real and worth half a basis point

Section 44's persistence result survives every correction: it uses no fitted
parameter, no band conditioning, and reproduces at t = 3.3 to 9.3 on 8,600
windows. So the natural question is whether it is worth anything *anywhere* —
Polymarket charges `7% × (1-p)` of stake, but the same five minutes of BTC trade
on Binance at about 4 basis points round trip, a thousand times cheaper.

Go long the direction the window has already moved, hold to the close:

```
asset     j   trades  mean bps       t   after 4bps
BTCUSDT   1    8,236    -0.137   -1.11      -4.14
BTCUSDT   3    8,508    +0.103   +1.30      -3.90
ETHUSDT   3    8,563    +0.245   +2.25      -3.76
ETHUSDT   4    8,587    +0.162   +2.14      -3.84
SOLUSDT   2    8,109    +0.484   +2.63      -3.52
SOLUSDT   4    8,239    +0.233   +2.45      -3.77
```

**0.08 to 0.48 basis points.** Statistically present in ETH and SOL at t > 2,
and eight to fifty times too small to pay a fee that is itself a rounding error
next to Polymarket's.

### Why a real effect is worth nothing

Sign persistence and expected return are different quantities, and this is the
gap between them. The Brownian baseline `1/2 + arcsin(sqrt(t/T))/pi` scores only
*which way* the window closes. A price can keep its direction 2.3 points more
often than a martingale allows while earning nothing, provided the extra
continuations are small and the reversals are large — which is exactly what the
numbers say. **A statistically solid edge in the sign carries no edge in the
money.**

That is the last open thread from section 44, and it closes the momentum line
completely:

- The persistence is real — 8,600 windows, nothing fitted.
- It is worth under half a basis point.
- Polymarket's taker fee is ~350 basis points of stake at the money.
- Binance's round trip is ~4.
- It does not clear either, and it misses Polymarket by three orders of
  magnitude.

The book was never mispricing anything worth having.

---

## 53. There are no liquidity rewards on the crypto hourlies

Polymarket runs a maker-incentive program, and the whole maker case would look
different if it paid here: rewards are income that does not care whether the
spread survives adverse selection. The market metadata answers it.

Every market carries `rewardsMaxSpread` (4.5¢) and `rewardsMinSize`, which look
like an active program but are defaults present everywhere. The field that says
a pool actually exists is `clobRewards` with a `rewardsDailyRate`:

```
slug                                           vol24h   rewards  minSize
will-there-be-no-change-in-fed-interest-rat 5,882,117  1,000.00      200
will-the-fed-increase-interest-rates-by-25- 4,636,106  1,000.00      200
lal-sev-val-2026-09-11-sev                  4,080,674      0.00       50
atp-zverev-khachan-2026-09-11               3,069,859      0.00       50
```

On `bitcoin-up-or-down-...-4pm-et` the key is **absent entirely**. The program
is real and pays $1,000 a day on the Fed markets; it pays nothing on the crypto
hourlies. A maker here earns the spread and only the spread, which is what
section 46 assumed and is now checked rather than assumed.

Two further constraints the metadata settles:

- `rewardsMinSize = 50` shares — about $30 a quote at 60¢, so two-sided
  qualifying size is ~$60. Against the $100 accounts this project is sized for,
  the program would be unreachable even where it pays.
- `orderMinSize = 5` shares (~$3) and a 1¢ tick, so the arms' $3 stakes sit
  exactly at the minimum tradable size. Nothing smaller is expressible.

### The capacity arithmetic, and why it implies the answer

That market shows $3,525 of book liquidity against $9,960 of 24-hour volume. A
$100 account is 2.8% of the book, and quoting at the touch earns better than
proportional fill share. Even at 3-5% of flow that is $300-500 of notional a
day, roughly 500-800 shares, and at the 1¢ half-spread that is $5-8 a day on
$100 — **5 to 8 percent per day, gross.**

A number that large is not an opportunity, it is a description of what adverse
selection must be taking. If posting at the touch here paid 5% a day net, the
spread would already be narrower. The gross figure is precisely why the markout
measurement is the only thing that matters, and it is what `makercheck.py` has
been running for the last hour.

---

## 54. What the real account's fee rate says, and why stale quotes cannot be the answer here

Section 1 verified the account this project started from: 14 days, $1.96M
turnover, **gross edge 1.64¢ a share, taker fees 1.16¢, rebate 0.34¢, net
0.82¢.** It pays $46,573 in taker fees, so it crosses the spread — it is a
taker. Every measurement in sections 41 through 52 says a taker has no edge.
Something has to give.

The fee rate itself narrows it. Fee per share is `0.07 × p × (1-p)`, so
1.16¢ solves to **p ≈ 0.79 or 0.21** — the account is not trading coin flips:

```
implied price 0.790   gross 2.08% of stake   fee 1.47%   implied win rate 0.807
implied price 0.210   gross 7.82% of stake   fee 5.53%   implied win rate 0.226
```

Against what taking the prevailing quote actually returns at those prices:

```
5-min book 0.7-0.8   ask 0.747 -> won 0.743   taker net -1.70c
5-min book 0.2-0.3   ask 0.245 -> won 0.246   taker net -1.13c
hourly     0.70+                              taker net -2.83c   t=-3.30
```

**A gap of about 2.5¢ a share** between what the account achieves and what the
displayed quote yields — more than a full spread. The account is not buying at
the price my harness sees.

### The obvious candidate, and why it fails

Quotes that have stopped updating are the natural explanation: a frozen ask
below fair value is free money to whoever crosses it first. This project's
`--max-stale` guard exists to *refuse* those, on the grounds that a frozen book
is not a tradeable price — which, if wrong, means the guard threw away the only
real edge. So `run.py` gained `--log-stale`, recording quote age on every sample
instead of discarding the observation, with `fresh` gating every path that
spends money so the guard itself is not removed to run the experiment.

But `stalecheck.py` already answered it, by polling the CLOB REST book whenever
the websocket said a token was frozen. Thirty stale observations:

| | n | |
|---|---|---|
| REST agrees, real quote present | 12 | a genuinely frozen market |
| REST agrees, book empty | 10 | nothing to hit |
| **REST disagrees** | **8** | **the feed is dead for that token** |

The disagreements are not small — websocket 0.35/0.36 against REST 0.21/0.22,
websocket 0.64/0.65 against REST 0.77/0.79. **Fourteen cents.**

So a stale quote is real about 40% of the time and a lie about 27%, and the lies
are exactly the favourable-looking ones: a price frozen where the market no
longer is. Paper-trading stale quotes would fill a quarter of the time at prices
that do not exist, at an average error many times the edge being hunted. That is
the artifact the guard was built for, and it is still there.

**This does not refute the stale-quote hypothesis — it says this harness cannot
test it.** Testing it honestly requires confirming each frozen quote against the
REST book before treating it as tradable, which is what a real bot would do and
what `--log-stale` alone does not. Recorded as the open question it is, rather
than answered with an instrument known to be lying 27% of the time.

The remaining candidates for the account's 2.5¢: it is partly a maker (the fee
figure is an average, and maker fills pay nothing, which would shift the implied
price), or it is faster than the book on a feed that does not go dead.

---

## 55. I reproduced the queuecheck bug in my own maker

The maker arms took **zero fills across 2,054 in-band quotes** in their first
half hour. The band was occupied 14.9% of the time, so the opportunity was
there; the fills were not.

The cause was in `maker_step`: it treated any change in the best bid as a cancel
and replace, resetting queue position. `queuecheck.py`'s own docstring says why
that is wrong, and says it about its own first two versions:

> Versions one and two both ended a level when the BEST BID PRICE changed, which
> is not what happens to an order. Somebody bidding higher does not cancel your
> order at 0.50 — it just stops being the best bid, and it still fills if the
> price comes back.

That diagnostic measured level lifetimes of 0.04s and a fill rate near zero,
both artifacts of the clock. I wrote the same mistake into the strategy four
sections later and got the same symptom — a near-zero fill rate — from the same
cause. Having written the lesson down is not the same as having learned it.

Fixed: a resting bid now lives until it fills or the mid drifts `--maker-cancel`
(default 4¢) away from it, which is what a real maker does. Maker mode also logs
`sample` rows now; the first version `continue`d past the logging, so those arms
recorded nothing for `--report` to score.

### The other thing zero fills would have meant

Worth separating, because the two failure modes look identical from outside:

- **Unfillable** — the queue never clears, so the strategy has no capacity
  regardless of its edge.
- **Adversely selected** — it fills, and the fills lose.

The measured trade rate makes the first a live concern independent of the bug:
**7 trades in 90 seconds across 12 tokens**, roughly one per token every 2.6
minutes, against resting size that a $3 order sits behind. A strategy whose edge
is 1¢ a share needs volume to matter, and this instrument may simply not have
it. `makercheck.py` reports both numbers — fill rate and markout — and it is the
fill rate that decides whether the markout is even worth reading.

---

## 56. Review: the harness was corrupting its own data, and `--report` was broken

> **Diagnosis corrected in section 60.** The drops are per-process capacity,
> not contention between collectors. Two arms alone drop at a higher rate than
> thirteen did.

Routine review, and two of the three things it found were faults in the
instrument rather than results from it.

### `--report` crashed on every pre-existing database

`--log-stale` added an `obs.stale` column with an `ALTER TABLE` migration in
`State.__init__`. `report()` opens the file directly and never builds a State,
so every database written before that column took the whole report down with
`no such column: stale`. **The one path every P&L conclusion in this project is
allowed to come from was dead for four arms and I did not notice until the
review asked for it.** Fixed by checking `PRAGMA table_info` first.

Worse, the identity check — the line that verifies pairs + residue reproduces
opens + hedges — had drifted *inside* the predicted-edge block during an earlier
edit, and referenced the hedge-leg totals. So on any arm that never hedges it
either crashed or silently never ran, **which is exactly the class of arm it
matters for**. It is now a top-level call with the hedge totals defaulted to
zero. All nine arms reconcile to $0.0000.

### The feed drops are self-inflicted, and they are the dead-feed artifact

Every one of the 309 logged errors across the fleet is the same:

```
[poly] ConnectionClosedError: received 1013 (try again later)
       slow consumer: send buffer full; retry
```

Polymarket is dropping the socket because the process is not draining it fast
enough. Counted per arm:

```
paper_dir 74   paper_lock 75   paper100_spot 58   paper100_fav 45
paper100_filt 37   paper_fav5 11   paper_dog5 9   paper_hour 0
```

This is almost certainly **section 54's dead feed**: `stalecheck.py` found the
websocket book disagreeing with the CLOB REST book 27% of the time, by up to 14
cents, and a dropped-and-resubscribed socket is precisely how a token's book
freezes in one view while the market moves on in the other.

It is self-inflicted. Thirteen collectors, each holding its own subscription to
overlapping token sets, on one machine. **The arms were degrading each other's
data quality**, which means every staleness measurement taken today is partly a
measurement of my own slowness.

### Cleanup, with a reason rather than tidiness

Retired four arms — `paper100_spot`, `paper100_fav`, `paper100_filt` and
`paper_lock`. The three `paper100_*` are filter variants of a strategy class
closed by sections 41, 42, 45 and 51, trading the same markets as `paper_dir`.
`paper_lock` differs from `paper_dir` only in lock mode, and section 40 proved
that pairing is pure regrouping — `NET = opens + hedges` identically — so the
pair carries nothing the single arm does not. Fleet is 13 → 9, and the four
heaviest subscribers are gone.

### Every arm, reconciled

```
arm              mkts    stake        NET       pct
paper_dir         222  27,281.12   -359.67    -1.32%
paper_lock        222  34,148.60   +145.62    +0.43%
paper100_spot     106   1,395.53    +50.26    +3.60%
paper100_fav       86     999.50     -0.21    -0.02%
paper100_filt      81     901.11    +15.54    +1.72%
paper_hour          6     199.50    -17.71    -8.87%
paper_fav5         27     433.01    -13.10    -3.03%
paper_dog5         21     304.97    -43.84   -14.37%
stale5              3      50.15    -10.47   -20.87%
```

The two 222-market arms are the line worth reading: **$27,281 and $34,149 of
turnover on identical markets, netting -1.32% and +0.43%.** Straddling zero on
that much turnover is what "no edge" looks like when it is finally measured
properly, and it is the same answer sections 41, 42, 45 and 51 reached from four
other directions.

### Samples that are not yet samples

- `paper_fav5` / `paper_dog5`: 27 and 21 markets. Both negative, as the
  complement constraint requires, and the 11-point gap between them is the right
  sign for the favourite effect — **and 27 markets says nothing**. No conclusion.
- `paper_hour`: 6 markets. No conclusion.
- `maker_fav` / `maker_dog`: relaunched 6 minutes ago after the queue fix, zero
  settlements. No conclusion.
- `stale5`: 3 markets, and its whole purpose is now in doubt given that the
  staleness it records is partly my own dropped sockets.

---

## 57. Making is closed: you only fill when you are swept

`makercheck.py` posted at the prevailing best bid on live hourly books for 75
minutes and measured where the mid sat after each fill.

```
=== 91 posts, 7 filled (7.7%) ===
  size ahead at post   median 100 sh   (p25 35, p75 233, max 952)
  markout   30s   n=7  mean -15.07c  median -16.50c  t=-2.40
  markout  120s   n=6  mean -21.50c  median  -6.00c  t=-1.74
```

A maker's entire gross edge here is the half-spread: 1.0¢ on hourly, 0.5¢ on
5-minute. **The markout is -15¢.** Adverse selection does not shave the edge, it
exceeds it by a factor of fifteen.

### The mechanism, fill by fill

```
  px   ahead   sold    30s      120s
 0.81      6     30  -0.165   -0.065
 0.71     20     22  +0.005   +0.035
 0.84    170    306  -0.275   -0.655
 0.57     20    120  -0.295   -0.545
 0.39     42     54  -0.365      n/a
 0.48      7     59  -0.005   -0.005
 0.47     18    196  +0.045   -0.055
```

Read `sold` against `ahead`. The fills that barely cleared the queue (22 vs 20)
have markouts near zero. The fills where selling ran to **many times** the size
resting ahead — 306 against 170, 120 against 20, 196 against 18 — are the ones
carrying -27¢, -30¢ and -37¢. Those are not trades, they are **sweeps**.

### Why a small maker cannot avoid this

The median size already resting at the touch is **100 shares**; the p75 is 233.
A $3 order is about 5 shares. Sitting behind 100 shares means ordinary two-way
flow never reaches you — only an order large enough to clear everything in front
does. **Queue position selects which flow you get, and a small maker's queue
position selects for sweeps exclusively.** You are not providing liquidity; you
are absorbing the tail of someone's market order, and that order exists because
its sender knows something.

The escape is to jump the queue by improving the bid a tick. But the spread is
1¢ and the tick is 1¢, so improving it hands over the entire gross edge. Section
51 already removed the other half of the maker case — the +4.3¢ "book bias" was
dwell-time selection and is really +0.39¢. So:

- **Behind the queue:** fill only on sweeps, markout -15¢.
- **Front of the queue:** pay the full spread to get there, edge 0.

There is no configuration in between, because the tick and the spread are the
same size. **Making is closed.**

### What this costs the other open question

The account from section 1 clears 1.64¢ a share gross while paying $46,573 of
taker fees. "Partly a maker" was one of the two remaining explanations, and a
-15¢ markout removes it. What is left is that it is faster than the book on a
feed that does not go dead — and section 56 established that my feed *does* go
dead, 309 times, from self-inflicted slow-consumer drops. **The one surviving
hypothesis is the one this harness is least equipped to test.**

### Caveat that matters

n = 7 fills. The t of -2.40 is not robust and three of the seven markouts are
near zero. What carries the conclusion is not the significance but the
**magnitude and the mechanism**: the edge is 1¢, the loss is 15¢, and the
sold-versus-ahead column says exactly why. An error large enough to reverse this
would have to be an order of magnitude.

---

## 58. The front-of-queue arm filled four times before any trade happened

Section 57 left one horn of its own argument unmeasured. Behind the queue you
fill only on sweeps and the markout is -15¢; the escape is to improve the bid by
a tick and take queue priority. Whether the front of the queue *also* gets picked
off decides whether making is merely unprofitable or actively adverse at every
position, so `--maker-improve` was added to post a tick above the touch.

It reported **4 fills in 90 seconds**, against 0 fills in 21 minutes for the
behind-the-queue arm. A 300-fold swing in fill rate from a one-tick change is
not a market phenomenon, it is a bug — and it was:

```python
if o["sold"] < o["ahead"]:
    return
```

Improving the bid puts the order at a price where nothing is resting, so
`ahead = 0`, and `sold >= ahead` is satisfied at `sold = 0`. **The order filled
before anyone had traded with it.** Fixed with `o["sold"] <= 0 or ...` — a fill
requires somebody to actually sell to you, at every queue position. Both maker
arms now report zero fills, which is what a 7.7% fill rate over ten-minute holds
predicts for a hundred seconds.

Worth noting what caught it: not a test, but the number being too good. A
strategy that starts filling 300× faster from a one-tick improvement is claiming
the queue does not exist. **Every artifact this project has found announced
itself the same way — as an unusually favourable number — and that remains the
only reliable detector here.**

---

## 59. A 50% drawdown limit delivered a 100% worst case

Reporting risk properly turned up two faults and one real result.

### The equity table is per-process, and reading drawdown off it is wrong

`paper_hour` showed **+$14.34** in the `equity` table against **-$17.71** from
`--report`. Not a contradiction: `st.equity` resets to the starting bankroll on
every restart and only records markets that settled while that process was
alive. `paper_hour` was restarted mid-session for the UMA settlement fix, so its
three rows cover three settlements out of six. `paper_dog5`, which never
restarted, matches `--report` to the cent (-43.84 = -43.84).

So drawdown is now rebuilt inside `report()` from the same settled markets as
the P&L, and the `equity` table is a diagnostic only. **One number, one source.**

### And the bankroll has to be stored with the data

The rebuilt figures first read 2.9%, 7.0% and 3.9% — because `--report` was
using argparse's default `--bankroll 1000` while every arm had run with 100.
A drawdown measured against the wrong capital is wrong by exactly that ratio.
`run.py` now writes a `meta` table at boot recording the bankroll and the full
argv, so a database describes the configuration that produced it.

Corrected:

```
arm            bankroll -> final    peak    trough   maxDD   worst mkt  mkts
paper_fav5      $100.00 -> $74.47  $100.07  $70.63   29.4%   -$10.02     39
paper_dog5      $100.00 -> $56.16  $120.13  $48.82   59.4%   -$10.03     21
paper_hour      $100.00 -> $82.29  $100.00  $61.34   38.7%   -$13.03      6
```

### The circuit breaker fired correctly and did not save the account

`paper_dog5` runs `--max-dd 0.5`, and the breaker worked exactly as written:

```
[HALT] drawdown 50.7% >= 50%; opening stopped, hedging continues
```

It still reached **59.4%**. Halting stops *opening*; it cannot stop capital
already committed from settling against you. At the moment it fired, $39.23 was
still at risk against $58.85 of equity.

Tracking, at every point in the run, what the drawdown would have been had the
open positions all settled to zero:

```
realised max drawdown            59.4%
halt threshold                   50.0%
worst case given open exposure  100.1%
```

**The 50% limit bought a strategy whose worst case was total ruin**, because at
one point committed capital equalled the entire account. The limit was never
wrong — it was measuring the wrong thing. A drawdown limit is a *reaction*; it
has no authority over money already spent.

### The control that was missing

Bounding simultaneous exposure is what bounds the overshoot, and it is a
separate control rather than a refinement of the drawdown limit. `run.py` gains
`--max-committed`, a cap on open exposure as a fraction of equity, applied at
every point capital is allocated (`free_capital()`). With the cap at 0.25, a
breaker at 50% has a worst case near 62%, instead of 100%.

This is the money-management lesson the project had not yet paid for: sizing
rules limit the loss per position, drawdown limits react to losses already
taken, and **neither one caps how much of the account is exposed at once.** That
needed its own control, and its absence is why a limit set at 50% could have
returned zero.

---

## 60. The 5-minute book pushes 19x the traffic, and that is the whole story

Section 56 blamed the 309 slow-consumer disconnects on thirteen collectors
contending for one machine. That was wrong, and the correction came free: after
cleaning down to two 5-minute arms, they dropped **twice each in eight minutes**
— a *higher* rate than the fleet of thirteen. Contention was never the variable.

Measuring what each subscriber actually has to absorb:

```
5-minute: 18 tokens →  867.5 frames/s   570.4 KB/s   1,683 level-updates/s
hourly:   12 tokens →   46.5 frames/s    28.6 KB/s      92.9 level-updates/s
```

**Nineteen times the frames, twenty times the bytes.** 867 frames per second is
867 `json.loads` calls plus 1,683 dictionary updates, in Python, in the same
event loop as a strategy pass every 250ms. The hourly arms have never dropped
once; the 5-minute arms dropped 74, 75, 58, 45 and 37 times. The instrument was
never overloaded by *how many* arms ran — a single 5-minute arm is already past
what one event loop absorbs comfortably.

### The fix, and why it is the right one

`websockets.connect` defaults to `max_queue=32`. When the consumer falls behind,
the client stops draining the TCP socket, the **server's** send buffer fills, and
Polymarket closes the connection with `1013 slow consumer`. Setting
`max_queue=None` lets the client keep draining into memory and absorb bursts
instead of pushing backpressure onto the server.

Applied. Two minutes later both arms show zero drops against a prior rate of
0.25/min — **which predicts only ~0.5 drops in that window, so this is not yet
evidence.** Recorded as a pending measurement, not a result.

### What it costs the earlier work

Every live 5-minute measurement in this project was taken through a feed that
drops under its own load, roughly once every five minutes on the long-running
arms. Each drop is followed by a resubscribe that misses whatever changed in
between — which is precisely how the websocket book came to disagree with the
CLOB REST book by 14¢ in section 54, and why "frozen quotes" appeared to exist.

The scoping this forces is worth stating plainly:

| measurement | feed | trustworthy? |
|---|---|---|
| §48/49 hourly calibration, 527 hours | CLOB REST history | **yes** — no websocket involved |
| §57 maker markout | hourly websocket | **yes** — hourly has never dropped |
| §46 hourly spread and mid travel | hourly websocket | **yes** |
| §42 5-minute book calibration | 5-minute websocket | **degraded** |
| §54 stale-quote rates | 5-minute websocket | **measures my own drops** |

The hourly results — which is where every surviving conclusion lives — are
clean. The 5-minute live results are contaminated to the extent the drops
matter, and their errors are large (14¢) and biased toward looking favourable.
That the two instruments disagree about staleness is not a mystery any more;
**one of them was being disconnected nineteen times more often because it was
being asked to absorb nineteen times more data.**

---

## 61. The venue basis is the TWAP lag, and it is worth 13 points of probability

These markets settle on Chainlink; the model and the book both watch exchange
prices. The `basis` table has been recording the gap all along and it had never
been read.

```
btc, 1,541 samples over 6.4h
  median +1.14 bps   p10 -2.98   p90 +4.90   |max| 22.74
```

A level difference is harmless: settlement compares the window's end against its
own open on the *same* source, so any constant basis cancels exactly. Only
movement **inside** the window reaches the payoff. So the quantity that matters
is how fast it moves:

```
basis change over    60s: sd 5.05 bps   mean +0.02
basis change over   300s: sd 5.15 bps   mean -0.01
basis change over  3600s: sd 5.17 bps   mean -0.04
```

**Flat across all three horizons.** The basis is not a drifting spread — it is
noise that fully decorrelates inside a minute. That shape is diagnostic: a
series that reaches its full variance by 60s and grows no further is the
signature of a **60-second TWAP lag**, which is exactly what
`btc-usd-twap-60s` is.

### What it would cost if unmodelled

Near the money the digital's sensitivity to a strike error is
`phi(0) / sigma_window`, so with a 5-minute window volatility around 12-16 bps:

```
0.4 x 5.05 / 12  =  16.8 points of probability
0.4 x 5.05 / 16  =  12.6 points
```

**Thirteen to seventeen percentage points of error on every near-the-money
quote**, from a five-basis-point price difference. That is the arithmetic behind
the earlier collapse in log-loss from 1.44 to 0.30 when the strike stopped being
a point sample and became the integral the settlement source actually uses. The
model was not slightly wrong before; it was wrong by more than the entire spread
it was hunting.

### Closed, not open

Two reasons this is not an opportunity. `fair_up` already integrates the
consolidated ticks across the averaging window (`i_known`), so the lag is
modelled rather than suffered. And the tick series is itself a **median across
Binance and Coinbase** — the consolidation happens before the model sees a
price. The 5 bps is measured *after* both of those.

What is left is genuine cross-venue disagreement against Chainlink's own
aggregate, and it is unpredictable at every horizon tested. It sets a noise
floor on this instrument that no model removes: **anyone pricing these
contracts, including the book, is working with a settlement reference they can
only estimate.** That is a reason the book is hard to beat, not a way to beat it.

---

## 62. The feed fix worked, and it reopens the only surviving hypothesis

> **Overstated — see section 71.** "Zero drops" was a 16-minute window. Over
> 89 arm-minutes the rate is 0.101/min against 0.222 before: a 55% reduction,
> not an elimination.

Section 60 removed the client-side receive-queue limit (`max_queue=None`) on the
grounds that the library's default 32-frame buffer was pushing backpressure onto
Polymarket's send buffer and earning a `1013 slow consumer` close. Two
independent checks now say it worked.

**Disconnects.** Three 5-minute arms have run since the change with **0
slow-consumer drops**, against a prior rate of 0.25/min per arm.

**The websocket book now agrees with the CLOB REST book.** `stalecheck.py`,
rerun unchanged except for the same one-line fix:

```
                       before      after
stale observations        30          16
  REST disagrees           8 (27%)     0 (0%)
  book empty              10           0
  real quote, confirmed   12          16
```

Fisher exact on the disagreement rate gives p ~ 0.04. The empty-book cases
vanished too, which fits: a book that looked empty was a book whose updates I
had missed. **Every websocket/REST disagreement in section 54 — up to 14 cents,
always in the flattering direction — was my own dropped socket.**

### What that changes

Section 54 concluded that stale quotes "cannot be tested with this harness"
because the instrument was lying a quarter of the time. That was true then and
is not true now. All 16 stale observations are **REST-confirmed resting quotes**
— a book that has sat untouched for 22 seconds at 0.49/0.50 is an order somebody
left there.

This matters because stale quotes are the **last surviving explanation** for the
account in section 1, which pays $46,573 of taker fees and clears 1.64c a share
where the displayed quote returns -1.70c. Section 57 killed "partly a maker"
with a -15c markout. What remained was "faster than the book on a feed that does
not go dead" — and the feed has just stopped going dead.

`stale5` is collecting again with `--log-stale 1`, and `report()`'s
taker-net-by-quote-age block will score it. The trading guard stays at
`--max-stale 20`, so the arm still refuses to trade what it is measuring.

### A note on what fixed it

The bug was one keyword argument, and it had been corrupting the project's
most-used instrument for its entire life. It was not found by inspecting the
code — it was found by asking why the hourly arms had **zero** disconnects while
the 5-minute arms had hundreds, and then measuring the difference: 19x the
frames, 20x the bytes. **The asymmetry was the clue, and it was in the logs from
the beginning.**

---

## 63. The tick grid makes passive making impossible, which is stronger than section 57

> **Overstated — see section 67.** The -0.5c penalty is not a property of the
> grid, it is `spread/2 - tick`, and those five fills all happened to sit on a
> 1c spread. A quarter of in-band quotes are 3c or wider, where a one-tick
> improvement still captures +0.5c to +3.0c.

Section 57 argued making was closed by a dilemma: behind the queue you fill only
on sweeps (-15c markout), and the escape — improving the bid a tick for queue
priority — "hands over the entire gross edge", leaving edge 0. The live arms say
the second horn is worse than that.

`maker_front` runs `--maker-improve 1`. Its fills, with `spread-capture` being
`mid - fill price`:

```
21:31:00  5pm-et  Up    px 0.56  sz 5.4  fee 0  spread-capture -0.0050
21:31:31  5pm-et  Up    px 0.56  sz 5.4  fee 0  spread-capture -0.0050
21:40:50  5pm-et  Up    px 0.62  sz 4.8  fee 0  spread-capture -0.0050
21:40:55  5pm-et  Up    px 0.56  sz 1.8  fee 0  spread-capture -0.0050
21:47:59  5pm-et  Down  px 0.59  sz 5.1  fee 0  spread-capture -0.0050
```

**Every capture is negative.** The spread is 1c and the tick is 1c, so the best
bid sits half a cent below the mid and improving it by one tick lands half a
cent *above*. There is no "post at the mid" option — **the grid does not contain
it.** A front-of-queue maker is not forgoing the spread, it is paying half of
one.

Meanwhile `maker_fav`, resting at the touch, has **0 fills** where
`maker_front` has 5 and `maker_dog` 1. Queue position determines fill rate
completely, exactly as section 57 said — but the price of leaving the back of
the queue is not zero, it is -0.5c.

So the dilemma is tighter than it looked:

| position | fills | economics |
|---|---|---|
| at the touch | essentially none | +1.0c if it ever filled, -15c markout when it does |
| one tick up | readily | **-0.5c before anything else happens** |

Against a book whose mid is biased by +0.39c (section 51, weighted the way a
trader is actually exposed), paying 0.5c above it is a loss on arrival. **Making
is not marginal here, it is arithmetically closed by the tick size.**

This is why the instrument matters more than the strategy: a 1c tick on a 1c
spread leaves no passive price that is both fillable and profitable. It would
take either a finer tick or a wider spread, and section 46 measured the hourly
spread at exactly 2c two-sided — one tick each side.

---

## 64. The exposure cap binds, and it quantifies what was wrong before

Section 59 added `--max-committed` after a 50% drawdown limit produced a 100.1%
worst case. The control is now running on the complementary pair at 0.25, with
the uncapped versions preserved for comparison:

```
                    max committed   mean committed
paper_fav5  (0.25)      $15.04          $7.52      ceiling $25 -- binding
paper_dog5  (0.25)      $15.65         $10.99      binding
paper_fav5_nocap        $82.73         $52.15
paper_dog5_nocap        $79.94         $48.09
paper_hour  (no cap)    $76.82         $52.33
```

**The uncapped arms carried 77-83% of the account in open positions at their
peak, and around half on average.** That is the number behind section 59's
finding: with four-fifths of capital already spent, a drawdown limit has
authority over the remaining fifth and nothing else.

Nothing here says the capped arms will make money — they are running a rule
section 51 showed to be noise. What it says is that the *account* now survives
being wrong, which is a separate property from being right and the only one this
project has been able to engineer deliberately.

Two settlements each so far, so the cap's ceiling has not been tested against a
busy stretch. Recorded as confirmed-binding, not as validated.

---

## 65. Exposure-weighted, the hourly taker loses and the maker earns exactly the spread

Section 51 established that hour-clustered equal weighting is not the estimand a
trader gets: it gives each market one vote regardless of how long it was
tradable, and dwell time is a function of the outcome. It applied that
correction to the "book bias" column and left the other two alone. Applying it
to all three:

```
                          obs-weighted    hour-clustered
vs mid                      +0.0039         +0.0448   t=+4.03
taker net (spread + fee)    -0.0227         +0.0181   t=+1.63
maker net (earns spread)    +0.0139         +0.0548   t=+4.93
```

**The taker column changes sign.** Section 49 reported +1.81c a share at t=1.63,
which read as "positive but not significant". Weighted by actual exposure it is
**-2.27c** — the same answer sections 41, 42, 45 and 51 give, now from the
cleanest dataset in the project (527 hours, 93k quotes, REST history rather than
a websocket).

And the maker figure lands exactly where the mechanism says it should:

```
half-spread                 +1.00c
book bias (exposure-wtd)    +0.39c
                            ------
maker gross                 +1.39c   <- measured: +1.39c
```

That is not a coincidence, it is a consistency check passing. The maker's income
**is** the spread plus whatever the mid is wrong by, and both terms are now
measured independently and add up.

### So the full picture, all exposure-weighted

| | per share |
|---|---|
| book mid's error | +0.39c |
| taker: crosses the spread, pays `7%x(1-p)` | **-2.27c** |
| maker: earns the spread, pays no fee | **+1.39c** *in theory* |
| maker: at the touch, when it actually fills | **-15c** (§57, sweeps) |
| maker: one tick up, where it does fill | **-0.5c on entry** (§63, tick grid) |

The theoretical maker edge is real and it is 1.39c. It is also unreachable: the
only two prices the grid offers are one where you do not fill except on sweeps,
and one that already costs half a cent more than the edge is worth.

**Every route through this instrument is now measured and every one is
negative.** Not "unproven" — measured, on the largest and cleanest sample the
project has, with the weighting a trader actually experiences.

---

## 66. Review: three conclusions overturned, none of them about the market

### (1) Health

Eight arms, no crashes, no exceptions. Ledger identity `$0.0000` on every arm
with settlements. Slow-consumer drops since the `max_queue` fix: **1 across all
arms in 16 minutes**, against a prior 0.25/min per arm.

### (2) Every arm, from `--report`

```
arm                mkts    stake      NET      pct    maxDD    final
paper_hour            6   199.50   -17.71   -8.87%   38.7%    82.29
paper_fav5            4    50.12   -32.29  -64.44%   32.3%    67.71
paper_dog5            5    50.08   +45.50  +90.85%    0.0%   145.50
stale5                3    25.12   -13.61  -54.17%   13.6%    86.39
paper_fav5_nocap     39   627.72   -25.53   -4.07%   29.4%    74.47
paper_dog5_nocap     21   304.97   -43.84  -14.37%   59.4%    56.16
```

`paper_fav5` at -64% and `paper_dog5` at +91% are complements of each other on
**four and five markets**. That is $3 flat stakes on a $100 account over a
handful of coin flips, and it is worth writing down only as a reminder of what
this sample size produces: two numbers that look like enormous results and mean
nothing. **No conclusions from any capped arm.**

### (3) What was overturned — all three were faults in the instrument

**Section 49's taker column changed sign.** +1.81c a share hour-clustered,
**-2.27c** weighted by actual exposure. Section 51 had already established that
equal-weighting markets is the wrong estimand; I applied the correction to one
column of that table and left the other two. Same data, same band, opposite
conclusion.

**Section 56's diagnosis was wrong.** It blamed 309 disconnects on thirteen
collectors contending. After cleaning to two arms they dropped *more often* —
the variable was per-process capacity, and the 5-minute books push 19x the
frames of the hourly ones.

**Section 54's "stale quotes cannot be tested here" is no longer true.** It was
true when written: the websocket disagreed with the REST book 27% of the time.
One keyword argument later it disagrees 0 times in 16 observations.

None of these were discoveries about Polymarket. All three were the harness
being wrong in a way that looked like a market fact.

### (4) Adjustments

**Retired `paper_hour`** — it ran with no `--max-committed` and was carrying
$83.98 of open exposure against $114.34 of equity, a configuration sections 59
and 64 now establish as unsafe, to test hourly taking, which section 65 measures
at -2.27c a share on 527 hours. Closed thesis at 73% exposure.

**Retired `maker_fav`** — 0 fills in 53 minutes. It tests the at-the-touch horn
of section 57, which `makercheck.py` has already measured directly (7.7% fill
rate, -15c markout). An arm producing no observations per hour is not a slow
experiment, it is not an experiment.

**Kept**: `maker_front` (the horn *not* yet measured, 5 fills and settlements
imminent), the capped complementary pair, and `stale5` — the live test of the
last surviving hypothesis, now that the feed can be trusted.

**`pickoff` finished with zero candidates** across its full run: no hourly quote
was ever both untouched for 5+ seconds and more than 3c from model fair. On the
hourly instrument, the pick-off opportunity does not exist.

### Sample sizes, stated plainly

`paper_fav5` 4 markets, `paper_dog5` 5, `stale5` 3, the maker arms 0
settlements. **Nothing in this round's P&L table supports a conclusion.** What
the round produced is three corrections and one closed avenue, and those came
from diagnostics, not from the ledger.

---

## 67. Section 63 generalised from five fills that shared a spread

Section 63 concluded that making is "arithmetically closed by the tick size",
because every `maker_front` fill showed a capture of exactly -0.0050: with a 1c
spread and a 1c tick, improving the bid lands half a cent *above* the mid.

The sixth fill showed `capture +0.0000`. That is not noise — it is the same
formula with a different input:

```
front-of-queue capture = spread/2 - tick
```

The first five fills all happened on a 1c spread. That is not what the book
does the rest of the time. Over 12,182 in-band hourly quotes:

```
spread   quotes   share   capture
   1c     6,039   49.6%    -0.5c
   2c     2,740   22.5%     0.0c
   3c     1,364   11.2%    +0.5c
   4c       479    3.9%    +1.0c
   5c       282    2.3%    +1.5c
   6c       220    1.8%    +2.0c
   7c       405    3.3%    +2.5c
   8c       374    3.1%    +3.0c
```

**A quarter of the time (25.6%) the spread is 3c or wider, and a one-tick
improvement still captures +0.5c to +3.0c** — averaging **+1.33c** across those
quotes. Section 63 took a property of half the book and asserted it of all of it.

### What that leaves

This is the first configuration in the project that is not closed on arithmetic:

```
capture when spread >= 3c        +1.33c   (exposure-weighted over those quotes)
book mid bias (section 65)       +0.39c
                                 ------
gross per fill                   +1.72c   with queue priority, no fee
```

Against which stands the only thing that has ever eaten a maker here: section
57's markout. That measurement was taken **at the touch**, where fills arrive
only via sweeps. Whether a front-of-queue order in a *wide* book is selected the
same way is a different question and an untested one — plausibly better, since
you are no longer the last stop of a sweep, and plausibly worse, since a wide
spread is what makers quote when they are uncertain.

`--maker-min-capture` gates on the capture actually available rather than
assuming the spread, and `maker_wide` is running it at 0.005 (quote only where a
tick still leaves half a cent). No fills yet: it quotes roughly a quarter of the
time and still needs someone to sell into it.

### The habit this keeps catching

Three sections in a row now have been corrected for the same reason, and it is
not carelessness about arithmetic — every one of them was arithmetically right.
It is **generalising from the cases the data happened to contain**: the cheap
pairs were all conditioned on having been right, the fast-exiting markets were
all conditioned on having resolved, and these five fills were all conditioned on
a 1c spread. The error is never in the calculation. It is in the silent "and
this is what always happens".

---

## 68. The spread is in equilibrium with the volatility, which is Glosten-Milgrom in one table

Section 67 found the first configuration not closed on arithmetic: quoting a
tick above the bid only where the spread is 3c or wider captures +1.33c on
average. The obvious objection is that makers widen when they expect to be run
over, so the capture is paid for in adverse selection. That is testable directly
from 106k hourly paired quotes — how far the mid travels after a quote, as a
function of the spread at that moment:

```
spread        n    |move| 30s   |move| 120s   front-of-queue capture
   1c    46,087       1.00c        3.00c            -0.5c
   2c    19,513       2.00c        4.50c             0.0c
   3c    14,646       2.00c        3.50c            +0.5c
   4c     6,905       1.50c        3.50c            +1.0c
   5c     5,079       1.50c        3.50c            +1.5c
   6c     4,142       2.00c        5.00c            +2.0c
   7c     4,644       2.50c        6.50c            +2.5c
   8c     2,102       3.00c        7.50c            +3.0c
```

Read the last two columns against each other at 5c and wider:

```
spread 5c   capture +1.5c   travel 1.5c
spread 6c   capture +2.0c   travel 2.0c
spread 7c   capture +2.5c   travel 2.5c
spread 8c   capture +3.0c   travel 3.0c
```

**Identical, to the cent, across four spread levels.** The market is quoting a
spread of almost exactly twice the 30-second mid travel — makers are pricing the
spread to compensate for exactly the move they expect while the order rests.

That is Glosten-Milgrom showing up in raw data: the spread is not a fee the
market charges out of habit, it is **the price of adverse selection**, and it is
set correctly here. It also explains why every maker configuration this project
has tried lands near zero before costs. There was never going to be a spread
wide enough to be free, because what makes a spread wide is exactly what makes
it necessary.

### What it means for `maker_wide`

The comparison above uses **unconditional** travel — how far the mid moves after
any quote. What a maker actually suffers is the move *conditional on having been
filled*, and a fill is someone choosing to trade with you, so conditional travel
is strictly worse. Capture equalling unconditional travel therefore implies
capture **below** adverse selection.

So the honest prediction for `maker_wide` is that it loses, but by much less
than at the touch: -15c (section 57) was a 1c-spread book where the capture is
-0.5c before anything happens. Here capture and volatility are matched, so the
loss should be the conditioning alone.

That is a falsifiable prediction with a sign and a rough size, made before the
arm has a single settlement, which is the most useful thing this project can do
with a hypothesis it expects to fail.

---

## 69. There are no stale quotes in tradeable markets. The last hypothesis is closed.

Section 62 reopened the stale-quote question: with the feed fixed, frozen books
became REST-confirmed rather than artifacts, and stale quotes were the only
surviving explanation for the section 1 account's 1.64c a share. `stale5` has
been recording quote age on the clean feed since.

**304 samples in actively trading 5-minute markets:**

```
  < 1s     296
  1-5s       8
  5-20s      0
  > 20s      0
  max      1.8s
```

Before the fix the same arm reported an average staleness of 3.2s and a maximum
of 18.7s. On a feed that is actually being drained, **the maximum quote age
across 304 observations is 1.8 seconds.** There is nothing to pick off.

### Reconciling this with section 62

`stalecheck.py` found 16 books untouched for 20+ seconds on the same clean feed,
six of them in a window that was nominally open. Both are true, and the
difference is which markets each instrument looks at:

- `stale5` samples only markets with `0 < tau < window` — **currently trading**.
- `stalecheck` subscribes across a wider slug range, including windows that have
  not opened yet and ones that have expired.

Nobody quotes a market that starts in ten minutes, so its book sits still. That
is a frozen book, it is REST-confirmed, and it is **not a price anyone can
trade**. The distinction the earlier sections kept missing is not
frozen-versus-live, it is *tradeable*-versus-not.

### What this closes

| explanation for the account's +1.64c gross | status |
|---|---|
| model edge over the book | no (§41) |
| the book being mispriced | no (§42, §45, §65) |
| partly a maker | no — -15c markout (§57) |
| liquidity rewards | none on these markets (§53) |
| picking off stale quotes | **no — quotes are never stale (this section)** |

**Every explanation this harness can test is now tested and negative.** The
account's 2.5c-a-share advantage over the displayed quote remains unexplained,
and the honest statement is that it is unexplained *by anything measurable from
outside* — not that it is impossible. What is left is latency at a scale this
setup cannot observe (its edge existing in the milliseconds between a Binance
print and a Polymarket quote update), or a difference in what it trades that the
public trade history did not reveal.

### The measurement that mattered most

This answer was only available after fixing one keyword argument. For the entire
life of the project before that, the instrument reported staleness that was
almost entirely its own — and that false signal is what made six earlier
findings look real, motivated the `--max-stale` guard, and kept a dead
hypothesis alive for fifty sections. **The single most valuable thing done today
was measuring the instrument instead of the market.**

---

## 70. A pre-registered prediction for the wide-spread maker

`makercheck.py` is running the section 67 configuration — post one tick above
the touch, only where the spread still leaves half a cent — and reports markout
at 30s and 120s. Writing the prediction down before it lands, because a result
that can be interpreted either way afterwards is not a test.

The capture available and the mid travel that has to be survived, by spread:

```
spread  capture    30s   ratio    120s   ratio
   3c    +0.5c   2.00c   0.25   3.50c   0.14
   4c    +1.0c   1.50c   0.67   3.50c   0.29
   5c    +1.5c   1.50c   1.00   3.50c   0.43
   6c    +2.0c   2.00c   1.00   5.00c   0.40
   7c    +2.5c   2.50c   1.00   6.50c   0.38
   8c    +3.0c   3.00c   1.00   7.50c   0.40
                         ----          ----
                    mean 0.82     mean 0.34
```

**The horizon decides it.** Against 30-second travel the capture is roughly
break-even (ratio 0.82, and exactly 1.00 at every spread from 5c up). Against
two-minute travel it is a third of what it needs (0.34).

Which means the question is not whether the spread compensates for volatility —
section 68 showed it does, at the 30-second horizon — but **how long a resting
order has to live before it fills**. A maker filling in 30 seconds breaks even.
A maker filling in two minutes is paid a third of the risk taken.

### The prediction

1. **Markout at 30s: near zero, within about ±0.5c.** The capture and the
   30-second travel are matched by construction of the spread.
2. **Markout at 120s: negative, roughly -60% of the capture taken.** Capture is
   0.34 of 120s travel, so about two thirds of the move is unpaid.
3. **Both far milder than section 57's -15c.** That figure came from the touch
   on a 1c-spread book, where fills arrive only via sweeps and the capture is
   -0.5c before anything happens. Here fills arrive from ordinary flow.

If instead the 30s markout comes back at -5c or worse, then front-of-queue in a
wide book is selected as brutally as the back of the queue in a narrow one, and
the spread's apparent fairness at 30 seconds is an illusion created by measuring
unconditional travel.

Either outcome is informative. The first says making here is a latency race with
a known finish line; the second says the flow is informed at every queue
position and the instrument is closed for good.

---

## 71. The feed fix halved the drops, it did not end them

Section 62 reported zero slow-consumer disconnects after setting
`max_queue=None`. That was a 16-minute window, and at the prior rate it should
have seen about four. Over a longer run:

```
                  drops   arm-minutes   rate
before max_queue     20            90   0.222/min
after                 9            89   0.101/min
```

**A 55% reduction, not an elimination.** The claim of zero was true of the
window I measured and false of the process. Removing client-side backpressure
lets the consumer absorb bursts; it does not make the consumer faster, and
867 frames/s with 1,683 level updates still outruns a Python event loop that
also has to run a strategy pass every 250ms.

Section 69's conclusion survives this, and survives it conservatively: a dropped
feed makes quotes look **more** stale, not less, so observing a maximum age of
1.8s across 304 samples *despite* three drops in that arm's lifetime bounds the
true staleness at or below that.

### The larger lever, which was sitting in plain sight

Each 5-minute arm subscribes to three windows — 18 tokens. The strategy trades a
market only when `0 < tau < window`, which is the **current** window alone. The
next one must be subscribed in advance so its strike can be caught in the two
seconds after it opens. The third is five to ten minutes out, is never traded,
and exists only to add frames to a stream that is already closing the socket.

`--windows` now controls the lookahead. `paper_fav5` is running at 2 (12 tokens)
against `paper_dog5` at the default 3 (18 tokens) — same markets, same strategy
family, different subscription load. The drop rates are the measurement.

That this was found by counting tokens rather than by profiling is the pattern of
the whole day: **the expensive bugs were all visible in the configuration, and
none of them were visible in the results they were corrupting.**

---

## 72. Review: the complementary-pair "instrument check" does not hold

### (1) Health

Five arms, no crashes, no exceptions, ledger identity `$0.0000` on every arm
with settlements. Drops: `paper_fav5` 1 in 9 minutes (0.111/min) at
`--windows 2`, `paper_dog5` 4 in 40 minutes (0.100/min) at `--windows 3`. **No
difference yet** — 9 minutes is far too short for that A/B and the rates are
indistinguishable.

### (2) Every arm, from `--report`

```
arm               mkts    stake      NET      pct    maxDD    final
paper_fav5          12   113.36   -23.58  -20.80%   32.3%    76.42
paper_dog5          14   126.84   +32.17  +25.36%   16.0%   132.17
maker_front          1    13.00    -7.92  -60.89%    7.9%    92.08
stale5              11    85.82    -1.88   -2.19%   35.3%    98.12
paper_fav5_nocap    39   627.72   -25.53   -4.07%   29.4%    74.47
paper_dog5_nocap    21   304.97   -43.84  -14.37%   59.4%    56.16
```

### (3) What was overturned: a safety property I had been asserting

Sections 43, 50 and 66, and the README, all claim the `--fav-only 1` / `-1` pair
"cannot both profit", and present that as a ledger audit that holds regardless
of what the market does. **It does not hold.**

```
markets traded:  fav 15   dog 17   shared 15
of the shared:   took the SAME side in 8, strictly opposite in 7
```

The reasoning was that at any instant the dearer and cheaper sides are opposite,
so the two arms take opposite positions. That is true **per quote** and false
**per market**: the favourite changes hands as the price moves, so an arm buying
"the dearer side" buys Up at 21:30 and Down at 21:40. Over a market's life both
arms accumulate both sides, and their P&L carries no constraint at all.

This round they came in at -20.80% and +25.36%. I would have read that as the
check passing. It is not a check — it is two loosely-related directional bets
that happened to land on opposite signs.

**What remains true** is the per-quote version: at a single instant the two rules
name opposite sides. That is a statement about the rules, not an audit of the
ledger, and it should never have been promoted to one. The genuine ledger audit
in this project is the identity check in `report()` — pairs + residue must
reproduce opens + hedges, because every share settles at 0 or 1. That one is
arithmetic and it has caught a real $46 error.

### (4) Samples

`paper_fav5` 12 markets, `paper_dog5` 14, `maker_front` **1**, `stale5` 11,
`maker_wide` 0. **No conclusion from any of them.** `maker_front` at -60.89% is
one market — three legs of one hour of one asset.

The only thing this round produced is the retraction above, and it came from
auditing a claim rather than from the P&L table. That is the third time today
that checking a *stated* property has been worth more than reading a result.

---

## 73. Cutting the subscription did not cut the drops

Section 71 identified what looked like an easy win: each 5-minute arm subscribes
to three windows (18 tokens) but only ever trades the current one, so the third
window is pure traffic. `--windows 2` cuts it to 12 tokens.

Within the same arm, same process configuration, only the lookahead changed:

```
paper_fav5  --windows 3   4 drops / 31 min = 0.129/min
paper_fav5  --windows 2   5 drops / 19 min = 0.263/min
```

**No improvement.** Four and five events is noise-dominated — observing 5 where
2.45 were expected is p ~ 0.08, so this is not evidence of harm either — but
there is no sign of the benefit the change was made for.

The explanation is in the thing I should have measured first: a market five to
ten minutes from opening has almost **no quote activity**. The 867 frames/s
comes overwhelmingly from the six tokens of the *current* window, where the
price is actually moving. Cutting twelve idle tokens removed twelve idle
subscriptions.

`--windows 2` stays, because the third window genuinely does nothing and costing
nothing is still better than costing a little. But **the drop rate is not a
subscription problem and cannot be fixed by subscribing to less.** It is the
intrinsic message rate of an active 5-minute book against a Python event loop,
and the only real fixes are a faster consumer or a separate process per feed.

### The pattern, again

This is the second optimisation today aimed at the drop rate, and the second
time the hypothesis was wrong: section 56 blamed contention between collectors
(it was per-process capacity), and this one blamed token count (it is message
rate per token). What actually halved the drops was `max_queue=None`, which
addressed neither — it just stopped the client from applying backpressure.

**Three guesses, one hit, and the hit came from reading the library's defaults
rather than from reasoning about load.** Worth remembering the next time a
performance story feels obvious.

---

## 74. Spreading across btc, eth and sol buys almost no diversification

`--max-committed` caps gross open exposure, and gross exposure is the right
quantity only if the positions are independent. These are not. Outcomes in the
same window, across 527 settled hours and 88 settled 5-minute windows:

```
                   hourly (527)     5-minute (88)
btc / eth agree       85.8%             80.7%
btc / sol agree       79.7%             88.6%
eth / sol agree       80.6%             78.4%
all three identical   73.1%             73.9%      (25% if independent)
implied rho            0.64              0.65
```

**Stable across a twelve-fold difference in horizon**, which is what a dominant
common factor looks like: three crypto Up/Down contracts on the same window are
three views of one question.

### What it costs

For N positions of size s with pairwise correlation rho, portfolio sd is
`s * sigma * sqrt(N(1 + (N-1)rho))`. At N=3, rho=0.64 that is 2.62, against 1.73
if independent:

- Three positions behave like **1.31 independent bets**, not 3.
- Risk is **1.52x** what the gross exposure suggests under independence.
- **Holding $3 in each of btc, eth and sol carries 87% of the risk of holding $9
  in one of them** — the diversification is worth 13%.

### The reframing that matters

`--max-committed 0.25` does not mean "25% of the account, spread across three
markets". It means **25% of the account in approximately one bet**. That is
still the right control and the right number — but it was chosen believing the
25% was diversified, and it is not.

This also explains a pattern the drawdown figures kept showing: `paper_dog5`
lost $10.03 on one market and $10.03 on the next, and `paper_hour` had a worst
single market of -$13.03 against a $100 account. Those were not independent
draws landing badly; they were the same draw, counted three times.

### And it is why the clustering rule exists

Section 43 corrected a t-statistic from +4.00 to +2.48 by clustering on the
**window** rather than the market, on the argument that btc, eth and sol resolve
the same five minutes of the same risk asset. That argument was made from first
principles and is now measured: rho = 0.64. The sqrt(3) correction was, if
anything, slightly too generous — the effective count is 1.31, not 1.

---

## 75. A restart wiped the drawdown breaker, and nobody noticed for four hours

`paper_fav5` runs `--max-dd 0.5`. `--report` puts it at **62.4% drawdown, $100
down to $37.64** over 23 settled markets. The breaker never fired.

The in-process ledger explains why: it showed 37.7% over 17 settlements. The arm
was restarted at 22:12 to change `--windows`, and `State.__init__` set
`self.equity = a.bankroll` and `self.peak = a.bankroll`. **The restart told the
account it was whole.** Every loss before 22:12 stopped existing as far as the
breaker was concerned, and it began counting from zero against a bankroll the
account no longer had.

This is section 59's error in a second place. There it was the *reporting* of
drawdown that was process-scoped; here it is the *control*. The same reset, and
this time it disabled a safety limit rather than misreporting one.

### The fix

`State._resume()` rebuilds equity and peak at boot from the settled markets
already in the file, the same way `report()` does — per market, payout minus
cost, in start-time order — so the breaker and the report cannot disagree about
what the account is worth. If the rebuilt drawdown is already past the limit, it
halts before placing anything.

Verified against the real file:

```
[resume] 23 settled markets in paper_fav5.db: equity $37.64 peak $100.00 drawdown 62.4%
[HALT] resumed already past the 50% drawdown limit; opening stays stopped

--report:  bankroll $100.00 -> $37.64   peak $100.00   max drawdown 62.4%
```

To the cent.

### How many restarts were there today

Arms were restarted for the UMA settlement fix, the `fav-only` gate, the
`max_queue` fix, the exposure cap, the phantom-fill fix, and `--windows`. **Every
one of those silently reset every running arm's drawdown limit.** The breaker has
been decorative for most of the session, which is worth stating plainly given
that risk control was an explicit requirement.

The reason it went unnoticed is that it fails silently and in the safe-looking
direction: a reset breaker never fires, and an arm that never halts looks like
an arm that never needed to.

### The general shape, for the third time

Sections 59, 75 and the `equity`-table confusion in between are all the same
bug: **state that belongs to the account was stored in the process.** Equity,
peak, drawdown, and the halt flag are properties of the money, not of the
program that happens to be managing it, and every one of them was being
reconstructed from a command-line default on every launch.

---

## 76. The prediction was wrong, in the way it said it might be

Section 70 pre-registered a prediction for the wide-spread front-of-queue maker.
The result:

```
48 posts, 14 filled (29.2%)
  improve 1 tick, min capture +0.005
  size ahead at post   median 0 sh
  capture at post      median +1.00c   mean +1.30c
  markout   30s   n=14  mean -7.500c   median  -8.000c   t=-2.27
  markout  120s   n=14  mean -9.357c   median -12.500c   t=-2.29
```

Against what was predicted:

| | predicted | observed |
|---|---|---|
| 30s markout | ~0, within ±0.5c | **-7.50c** |
| 120s markout | ~-0.8c (-60% of capture) | **-9.36c** |
| milder than §57's -15c | yes | yes, but only by half |

**Wrong on both numbers.** Section 70 also wrote down what being wrong would
mean, which is the only reason this is informative rather than embarrassing:

> If instead the 30s markout comes back at -5c or worse, then front-of-queue in
> a wide book is selected as brutally as the back of the queue in a narrow one,
> and the spread's apparent fairness at 30 seconds is an illusion created by
> measuring unconditional travel.

That is what happened.

### The adverse-selection multiplier

Section 68's equilibrium was real but was measuring the wrong quantity. The
unconditional 30-second mid travel at these spreads is ~1.5c. **Conditional on
having been filled it is 7.5c — five times larger.** Being filled is itself the
information: somebody chose to sell to you, and they chose because the move was
already coming.

So the full accounting for the best maker configuration this project found:

```
capture at post          +1.30c
adverse selection 30s    -7.50c
                         ------
                          -6.20c per fill
```

Queue priority worked exactly as designed — the fill rate went from 7.7% at the
touch to **29.2%** one tick up, and the spread gate delivered the +1.0-1.3c it
promised. Both halves of the mechanism did their job and the trade still loses
six cents a share.

### What this actually closes

**Making is closed at every queue position and every spread**, which was the last
route left:

| configuration | capture | markout | net |
|---|---|---|---|
| at the touch, any spread (§57) | +1.0c | -15c | -14c |
| one tick up, 1c spread (§63) | -0.5c | — | negative on entry |
| one tick up, spread >= 3c (this) | +1.3c | -7.5c | **-6.2c** |

### The caveat that is also the answer

This simulation never cancels. It posts, fills, and holds to settlement — so it
eats the entire post-fill move. A real market maker cancels and reprices
continuously, and the -7.5c at 30 seconds is only paid by someone who cannot
react inside 30 seconds.

That reframes the whole maker question. **The spread does not compensate a maker
for being filled; it compensates a maker for being fast.** Glosten-Milgrom says
the spread prices adverse selection given the maker's information; what section
68 measured is that the spread matches unconditional volatility, and what this
section measures is that the conditional move is 5x that. The gap between them
is precisely the value of being able to cancel — and it is about 6 cents a
share, on a 60-cent contract, per fill.

Which is a perfectly good description of why this instrument is unavailable to a
$100 account run from a Python event loop that drops its own websocket.

---

## 77. Review: down to one arm, because there is one question left

### (1) Health

No crashes, no exceptions, ledger identity `$0.0000` on every arm with
settlements. The round's health check found the drawdown breaker had been
disabled by restarts for most of the session (section 75) — a fault the ledger
was reporting correctly and that nothing else would have surfaced.

### (2) Reports

```
arm            mkts    stake      NET      pct    maxDD    final
paper_fav5       23   210.78   -62.36  -29.58%   62.4%    37.64
maker_front       1    13.00    -7.92  -60.89%    7.9%    92.08
observer          -        -        -        -        -        -
```

`maker_front` is one market. `paper_fav5` is 23, and its interest is not the
-29.58% but the 62.4% drawdown that a 50% limit failed to stop.

### (3) What was learned

The pre-registered prediction failed (section 76) and named its own failure
mode in advance. **Conditional post-fill movement is 5x unconditional movement**
— 7.5c against 1.5c — so the spread-equals-volatility equilibrium of section 68
was true and irrelevant. Making is closed at every queue position and spread.

### (4) Fleet

Retired `maker_wide`, `maker_front` and `paper_fav5`. All three test theses now
closed by direct measurement, and `paper_fav5` was halted at 62.4% drawdown
anyway. `maker_wide` produced 0 fills in 50 minutes against `makercheck`'s 14 in
45 — when a diagnostic answers the same question an order of magnitude faster,
keeping the slow arm is sentiment, not science.

**One arm remains: `observer`.** It does not trade at all (`--min-edge 99`) and
exists for one measurement — section 42's book calibration was taken on the
broken feed, and every clean-feed arm since has carried `--fav-only`, which
gates *before* the sample is written and therefore records only the dearer side.
The observer logs both (50 Up, 50 Down so far), which is the unselected
population section 42 needs to be re-tested against.

That is the honest state of the project: **every strategy route is closed, and
the single open question is whether a foundational measurement survives being
re-taken on an instrument that works.**

### Samples, stated plainly

`observer` has 100 samples and 0 settled markets. Section 42 used 5,373 over
168. **Nothing is concludable this round**, and the next several rounds will
also conclude nothing, because the only remaining measurement needs roughly a
day of collection to be comparable.
