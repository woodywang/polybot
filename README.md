# polybot

Research harness for Polymarket's crypto Up/Down markets — 5-minute and hourly.

It prices the contracts against a consolidated Binance + Coinbase feed, paper
trades several strategies side by side, and reports calibration and P&L against
actual settlements. **No orders are sent.**

[FINDINGS.md](FINDINGS.md) is the real output: 51 sections, including every
conclusion this project has had to retract — which is most of the interesting
ones.

## What it concluded

**There is no taker edge, and the reason is arithmetic.** Measured three
independent ways:

| measurement | result |
|---|---|
| model vs book, 3,000 traded legs | no relationship between predicted and realised |
| book vs outcome, 5,373 unselected quotes | book calibrated: 0.245→0.246, 0.644→0.633, 0.955→0.961 |
| book vs outcome, 93k hourly quotes over 22 days | same, and significantly *negative* above 0.70 |

The taker fee is `7% × (1-p)` of stake — 3.5% at the money — against a book that
prices correctly. Nothing downstream of that changes the sign.

**Five-minute crypto does have real momentum.** Tested with no volatility
estimate at all, using the fact that for Brownian motion the chance a move's
sign survives depends only on the fraction of the window elapsed:

```
P(W_T > 0 | W_t > 0) = 1/2 + arcsin(sqrt(t/T)) / pi
```

Against 30 days, actual persistence exceeds it by 1.3 to 2.3 points, t = 3.3 to
9.3 on 8,600 windows. **The book prices it. The random walk does not.**

## Three ways the same trap was fallen into

Every headline result here died the same death: a quantity that looks like a
price turned out to be a summary of the path that produced it.

- **Cheap hedged pairs** ($0.84 per $1 "locked profit") — you only get a cheap
  pair when the first leg already moved your way. Conditioned on being right.
- **"ask > 0.5" as the favourite** — the two asks sum to $1.035, so near the
  money both sides qualify and the same instant counts twice.
- **Dwell-time selection** — a market leaves a price band *because* it resolved,
  so equal-weighting markets over-weights the ones that resolved hardest. A
  +4.5-point book bias at t = 4.0 became +0.4 points when weighted the way a
  trader is actually exposed.

## Method that survived

- P&L conclusions come only from `run.py --report`. Three ad-hoc scripts once
  produced −14.6%, +8.9% and an actual −1.14%.
- `report()` prints an identity check: regrouping legs into pairs + residue must
  reproduce open legs + hedge legs, because every share settles at 0 or 1. It
  was silently $46 short on a $797 book.
- btc, eth and sol resolve the same window of the same risk asset. Cluster on
  the window, not the market, or t is inflated by √3.
- A staleness guard (`--max-stale`) on every arm. Frozen books once produced a
  45-cent "mispricing" and a 100% win rate, both artifacts.
- Complementary arm pairs (`--fav-only 1` and `-1`) that **cannot both profit**,
  so the ledger audits itself regardless of what the market does.

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
run.py          feeds, discovery, taker + maker strategies, FIFO matching, reporting
momentum.py     sigma-free persistence test on 30 days of klines
histtest.py     22 days of hourly book quotes vs settlement; maker backtest
makercheck.py   markout on simulated resting bids — does a fill cost more than it pays?
stalecheck.py   websocket book vs CLOB REST
queuecheck.py   queue lifetime at the touch
```

`*.db`, `*.log` and `*.json` data files are run artifacts and are not tracked.
