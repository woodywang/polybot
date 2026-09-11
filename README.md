# polybot

Research harness for Polymarket's 5-minute crypto Up/Down markets.

It prices the contracts against a consolidated Binance + Coinbase feed, paper
trades two strategies side by side, and reports calibration and P&L against
actual settlements. **No orders are sent.**

See [FINDINGS.md](FINDINGS.md) for what the data actually says — including the
parts that overturned earlier conclusions.

## Why the textbook model is wrong here

These markets settle on a Chainlink TWAP of the window compared against the
price at the window's open, so the payoff is a digital on an **average**, not
on the terminal price:

```
tau >= w:   mean = S,                      var = sigma^2 (tau - 2w/3)
tau <  w:   mean = (I_known + S*tau) / w,  var = sigma^2 tau^3 / (3 w^2)
```

The practical consequence, from `fair.py`'s self-check:

```
ATM rally : twap 0.4818 vs naive 0.5420  -> naive overpays Up  +0.0602
ITM late  : twap 0.9888 vs naive 0.8541  -> naive underpays Up +0.1347
same spot, same tau, different path: 0.4276 .. 0.7081  (spread 0.2805)
```

A spot model cannot express that last line at all.

## Taker fee

Verified to 5 decimal places against 148k real fills:

```
fee = shares × 0.07 × p × (1-p)        =>   fee as % of stake = 7% × (1-p)
```

It peaks at the money, which is where a coin-flip strategy lives.

## Run it

```bash
./scripts/env.sh python3 fair.py                      # model self-check
./scripts/env.sh python3 run.py --db paper.db         # collect
./scripts/env.sh python3 run.py --report --db paper.db
```

`scripts/env.sh` is the only entry point; it builds
`contrib/Dockerfile.buildenv` and runs the command inside it. Nothing is
installed on the host.

## Strategies

| mode | rule |
|---|---|
| `--lock-mode cost` | hedge whenever the pair comes in under $1 |
| `--lock-mode edge` | hedge only when the hedge leg is itself +EV |
| `--lock-mode both` | require both |

Locking beats holding exactly when `(1 - p) > ask + fee`, i.e. when the hedge
leg is a positive-edge purchase on its own. `cost` ignores this and therefore
fires on winners too.

## Knobs

| flag | what it does |
|---|---|
| `--kelly` / `--bankroll` | fractional Kelly on the directional leg |
| `--shrink` | pull the model probability toward the book's own price |
| `--min-p-lock` | refuse legs a hedge probably cannot reach before expiry |
| `--er-min` / `--er-max` | Kaufman efficiency ratio gate (direction) |
| `--path-min` | distance walked, in bps (amplitude) |
| `--min-intensity` | relative traded notional |
| `--latency-ms` | round trip modelled before a simulated fill |

## Layout

```
fair.py     TWAP-digital model, taker fee, random-walk risk controls, self-check
run.py      feeds, market discovery, both strategies, FIFO matching, reporting
scripts/env.sh
contrib/Dockerfile.buildenv
```

`*.db` and `*.log` are run artifacts and are not tracked.
