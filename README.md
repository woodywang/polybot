# polybot

Research harness for Polymarket's crypto Up/Down markets — 5-minute and hourly.

It prices the contracts against a consolidated Binance + Coinbase feed, paper
trades several strategies side by side, and reports calibration, P&L and risk
against actual settlements. **No orders are sent.**

[FINDINGS.md](FINDINGS.md) is the real output: 69 sections, including every
conclusion that had to be retracted — which is most of the interesting ones.

## What it concluded

**No route through this instrument is profitable, and each one is closed for a
different reason.**

| route | verdict | why |
|---|---|---|
| take the quote | **-2.27c/share** | book is calibrated; fee is `7% x (1-p)` of stake |
| make at the touch | **-15c markout** | behind 100 shares you only fill when swept |
| make one tick up | capture = `spread/2 - tick` | negative or zero on 72% of quotes |
| make where the spread is wide | capture ≈ volatility | the spread *is* the price of adverse selection |
| collect liquidity rewards | none exist | `clobRewards` is absent on crypto hourlies |
| pick off stale quotes | **none exist** | max quote age 1.8s across 304 samples |
| trade the momentum | **+0.4 bps** | real, and 1000x too small for the fee |
| hour-to-hour reversal | priced | book opens at 0.5105 / 0.4812, not 0.50 |
| cross-asset lead-lag | priced | slope 0.08, t = 0.26 |
| beat the book on speed | ~302ms window | that is the competition's reaction time, not a gap |

## Where the edge actually is, and why it is not here

The book follows Binance with a measured lag. Corrected for this machine's own
feed delays — verified two independent ways, agreeing to 0.6ms — it is **~302
milliseconds**:

```
                      RTT/2     one-way from exchange timestamps
binance              115.2ms          115.8ms
polymarket            21.0ms           13.4ms
```

In the top decile of volatility 302ms is worth 2.17c a share, which is the only
figure in this project within range of the real account's measured 1.64c gross.
It is also the only region the harness could not examine: a 302ms window against
a 250ms strategy loop and a staleness guard set at 20 seconds.

**But 302ms is not a gap, it is an equilibrium.** The book moves at 302ms because
that is when the fastest participants have finished trading. Arriving then is
arriving as the opportunity closes — to profit you must beat the marginal
participant who is already setting that number, and no measurement here can see
them.

## The one clean piece of finance in it

Quoting the hourly book by spread, against how far the mid then travels:

```
spread   front-of-queue capture   |mid move| in 30s
  5c            +1.5c                  1.5c
  6c            +2.0c                  2.0c
  7c            +2.5c                  2.5c
  8c            +3.0c                  3.0c
```

Identical to the cent across four levels. **The spread is not a fee, it is the
price of adverse selection**, quoted at about twice the 30-second volatility —
Glosten-Milgrom falling out of 106k raw quotes. There was never going to be a
spread wide enough to be free, because what makes a spread wide is exactly what
makes it necessary.

## The number that explains all of it

The fee is a fixed **1.40 probability points per trade** at any horizon
(`0.056 x p(1-p)` at the money). What changes with horizon is the price precision
that buys those points — and the settlement reference has its own irreducible
noise of **5.05 bps** (the Binance-vs-Chainlink basis, unpredictable at 60s, 300s
and 3600s alike):

```
 horizon   precision needed   settlement noise   ratio
  5 min        0.49 bps           5.05 bps        0.10
  1 hour       1.69 bps           5.05 bps        0.33
  1 day        8.27 bps           5.05 bps        1.64
 30 days      45.28 bps           5.05 bps        8.97
```

**A 5-minute contract asks you to resolve the price ten times finer than its own
settlement source is defined.** Not because the book is clever — because the
quantity is not knowable to that precision. Everything else in this file is
downstream of that one line.

Crypto is also the **most expensive category on the platform**: 7% against 3-5%
elsewhere, with the lowest rebate rate. Net of rebate, 2.80% of stake at the
money versus 1.12% on the cheapest.

## The instrument that should have worked

`btc-multi-strikes-weekly` — daily events, 11-15 strikes, settling on the
**Binance 1-minute candle at noon ET** (verified at 0.00% disagreement over 1,581
markets), 10x the liquidity, and a horizon where the fee arithmetic is sane.
Seven expiries live at once, so there is a full surface.

It was checked for everything:

| test | needs a model? | result |
|---|---|---|
| implied vol vs subsequent realised | yes | unbiased within ~5 points |
| ladder calibration, tails | yes | right to 1 point |
| strike monotonicity | **no** | zero violations |
| calendar (total variance) | **no** | zero violations |
| forward level | no | within $140 of spot, 7 expiries |
| term structure | yes | prices mean reversion, 18.5% → 32.8% |

Arbitrage-free in strike, arbitrage-free in time, flat forward, unbiased
forecast, calibrated tails. **There is no edge here because there is no mistake
here.**

## Momentum is real and worthless

Tested with no volatility estimate at all, using the fact that for Brownian
motion the chance a move's sign survives depends only on the fraction elapsed:

```
P(W_T > 0 | W_t > 0) = 1/2 + arcsin(sqrt(t/T)) / pi
```

Over 30 days, actual persistence exceeds it by 1.3 to 2.3 points, t = 3.3 to
9.3 on 8,600 windows. Converted to money it is **0.08 to 0.48 basis points** —
against 4 bps of Binance fees and 350 bps of Polymarket's. A statistically solid
edge in the *sign* carries no edge in the *money*.

## Four ways the same mistake was made

Every headline result here died the same death: a quantity that looks like a
price turned out to be a summary of the path that produced it.

- **Cheap hedged pairs** ($0.84 per $1 "locked profit") — you only get a cheap
  pair when the first leg already moved your way.
- **"ask > 0.5" as the favourite** — the two asks sum to $1.035, so near the
  money both sides qualify.
- **Dwell-time selection** — a market leaves a price band *because* it resolved,
  so equal-weighting markets over-weights the ones that resolved hardest. A
  +4.5-point book bias at t = 4.0 became +0.4 points weighted by exposure.
- **A spread that was always 1c** — five fills sharing one spread became a claim
  about the whole book.

The calculation was right every time. The error was always the silent "and this
is what always happens".

## The bug that mattered most

`websockets.connect` defaults to `max_queue=32`. The 5-minute books push 867
frames/s and 570 KB/s — 19x the hourly markets — so the consumer fell behind,
stopped draining the socket, and Polymarket closed it with `1013 slow consumer`
309 times. Each drop was followed by a resubscribe that missed everything in
between.

That manufactured "stale quotes" out of nothing, made the websocket book
disagree with the REST book by up to 14c (always flatteringly), and kept a dead
hypothesis alive for fifty sections. After `max_queue=None`: REST disagreements
went 8/30 to 0/16, and maximum observed quote age went from 18.7s to 1.8s.

**It was found by asking why the hourly arms had zero disconnects while the
5-minute arms had hundreds.** The asymmetry was in the logs from the beginning.

## Risk control, and what it taught

A 50% drawdown limit fired correctly at 50.7% and the account still fell 59.4%,
because halting stops *opening* and has no authority over capital already
committed. Tracking what would have happened had the open positions settled to
zero gives a worst case of **100.1%** — total ruin from a limit set at 50%.

The uncapped arms carried **77-83% of the account** in open positions at peak.
`--max-committed` caps simultaneous exposure and is a separate control, not a
refinement of the drawdown limit:

> Sizing rules limit the loss per position. Drawdown limits react to losses
> already taken. **Neither caps how much of the account is exposed at once.**

## Method that survived

- P&L conclusions come only from `run.py --report`. Three ad-hoc scripts once
  produced -14.6%, +8.9% and an actual -1.14%.
- `report()` prints an identity check: pairs + residue must reproduce open legs
  + hedge legs, because every share settles at 0 or 1. It was silently $46 short
  on a $797 book, and later silently stopped running on arms that never hedge.
- Drawdown is rebuilt from the same settled markets as the P&L. The `equity`
  table is per-process and resets on restart; reading risk off it reported
  +$14.34 where the truth was -$17.71.
- A `meta` table records the bankroll and full argv, so a database describes the
  configuration that produced it. Without it, `--report` measured drawdown
  against argparse's default and was wrong by 10x.
- btc, eth and sol resolve the same window of the same risk asset. Cluster on
  the window, not the market, or t is inflated by sqrt(3).
- The identity check above is the real ledger audit. A complementary arm pair
  (`--fav-only 1` and `-1`) is **not** one: the two rules name opposite sides at
  any instant, but the favourite changes hands during a market, so both arms end
  up holding both sides and their P&L carries no constraint (§72).
- Every artifact found so far announced itself as an unusually favourable
  number. That remains the only reliable detector.

## Run it

```bash
./scripts/env.sh python3 fair.py                         # model self-check
./scripts/env.sh python3 run.py --db paper.db            # collect
./scripts/env.sh python3 run.py --report --db paper.db
./scripts/env.sh python3 momentum.py                     # 30-day persistence
./scripts/env.sh python3 momentum.py hourly
```

`scripts/env.sh` is the only entry point; it builds `contrib/Dockerfile.buildenv`
and runs the command inside it. Nothing is installed on the host.

## Layout

```
fair.py         TWAP-digital model, taker fee, random-walk risk controls, self-check
run.py          feeds, discovery, taker + maker strategies, FIFO matching, risk, reporting
momentum.py     sigma-free persistence test on 30 days of klines
histtest.py     22 days of hourly book quotes vs settlement; maker backtest
makercheck.py   markout on simulated resting bids, at the touch or a tick up
pickoff.py      REST-confirmed mispriced-quote scanner (found none)
stalecheck.py   websocket book vs CLOB REST
queuecheck.py   queue lifetime at the touch
```

`*.db`, `*.log` and `*.json` data files are run artifacts and are not tracked.
