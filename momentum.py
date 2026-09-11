"""Is the favourite effect a property of 5-minute crypto, or of one afternoon?

Section 43 found the dearer side beating its own price at t ~ 1.8 on 64 windows.
"The favourite" is just "the side currently ahead", so five trending hours would
manufacture exactly that, and 64 windows cannot tell the two apart.

This can. Section 42 established the book is calibrated on average and section 41
that the model tracks the book, so testing the driftless random walk against 30
days of 1-minute klines asks the same question with 500x the sample: conditional
on being ahead with tau left, does the leader hold on more often than a
martingale says it should?

No book data is involved, so nothing here can be a book artifact -- and the
placebo re-runs the whole pipeline against shuffled window outcomes, which must
come back flat.
"""
import json, math, random, statistics, sys

WIN, BARS = 300, 5                    # window seconds / 1-minute bars in it
LOOK = 60                             # bars of trailing vol; swept in __main__
PHI = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def windows(bars):
    """(open, close, sigma_per_sqrt_sec, [(tau, spot), ...]) per aligned window."""
    by_ts = {b[0]: b for b in bars}
    out = []
    for i in range(LOOK, len(bars) - BARS):
        t0 = bars[i][0]
        if t0 % WIN:
            continue
        seq = [by_ts.get(t0 + k * 60) for k in range(BARS)]
        if any(s is None for s in seq):
            continue
        prev = [bars[j] for j in range(i - LOOK, i)]
        r = [math.log(b[2] / b[1]) for b in prev if b[1] > 0 and b[2] > 0]
        if len(r) < LOOK // 2:
            continue
        sd = statistics.stdev(r)
        if sd <= 0:
            continue
        o, c = seq[0][1], seq[-1][2]
        pts = [((BARS - k) * 60.0, seq[k - 1][2]) for k in range(1, BARS)]
        out.append((t0, o, c, sd / math.sqrt(60.0), pts))
    return out


def run(all_w, label, flip=None):
    """flip: optional {ts: close} override, used by the placebo."""
    cell = {}
    for t0, o, c, sig, pts in all_w:
        if flip is not None:
            c = flip[t0]
        for tau, s in pts:
            if s <= 0 or o <= 0:
                continue
            p_up = PHI(math.log(s / o) / (sig * math.sqrt(tau)))
            fav_up = p_up >= 0.5
            p = p_up if fav_up else 1.0 - p_up
            y = 1.0 if ((c > o) == fav_up) else 0.0
            k = 0 if tau < 90 else (1 if tau < 180 else 2)
            m = cell.setdefault(k, {}).setdefault(t0, [0, 0.0, 0.0])
            m[0] += 1; m[1] += y - p; m[2] += p
    print(f"\n=== {label} ===")
    print(f"  {'tau':<10}{'obs':>9}{'windows':>9}{'mean p':>9}"
          f"{'too timid':>12}{'t':>8}")
    for k in sorted(cell):
        v = [x[1] / x[0] for x in cell[k].values()]
        pm = [x[2] / x[0] for x in cell[k].values()]
        sd = statistics.stdev(v) if len(v) > 1 else 0.0
        t = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
        print(f"  {('60s','120s','180-240s')[k]:<10}"
              f"{sum(x[0] for x in cell[k].values()):>9,}{len(v):>9,}"
              f"{statistics.mean(pm):>9.3f}{statistics.mean(v):>+12.4f}{t:>+8.2f}")
    return cell


def sigma_free(raw):
    """The same question with no volatility estimate anywhere.

    Every version above prices the lead with a trailing sigma, and the sweep
    shows the answer moving with the estimator -- which is not a property of the
    market.  For Brownian motion the chance that the sign of the move survives
    to the end depends only on the fraction of the window elapsed:

        corr(W_t, W_T) = sqrt(t/T)
        P(W_T > 0 | W_t > 0) = 1/2 + arcsin(sqrt(t/T)) / pi

    No sigma, no estimation, no fitted parameter.  Whatever this says is a
    property of the price path and nothing else.
    """
    print("\n=== sign persistence vs the exact Brownian value (no sigma) ===")
    print(f"  {'elapsed':<10}{'windows':>9}{'':>11}{'':>9}"
          f"{'excess':>10}{'t':>8}")
    per = {}
    for sym, bars in raw.items():
        for t0, o, c, _sig, pts in windows(bars):
            for tau, s in pts:
                if s == o or c == o:
                    continue
                j = BARS - int(tau // 60)
                # Pool offsets into quarters of the window.  One row per offset
                # is 59 tests on 715 windows for the hourly case, which is how
                # a table of noise grows two-star cells.  The Brownian value
                # differs across the offsets inside a bucket, so subtract it
                # per observation and average the residual, never the rate.
                rho = math.sqrt(j / BARS)
                g = min(int(j * 4 / BARS), 3)
                per.setdefault(g, {}).setdefault(t0, []).append(
                    (1.0 if ((s > o) == (c > o)) else 0.0)
                    - (0.5 + math.asin(rho) / math.pi))
    for g in sorted(per):
        v = [statistics.mean(x) for x in per[g].values()]
        sd = statistics.stdev(v)
        d = statistics.mean(v)
        print(f"  {f'{g*25}-{g*25+25}%':<10}{len(v):>9,}{'':>11}"
              f"{'':>9}{d:>+10.4f}{d/(sd/math.sqrt(len(v))):>+8.2f}")


def by_price(all_w):
    """The same measurement bucketed by the favourite's price, net of the fee.

    The tau table hides the thing that decides tradability: the taker fee is
    7%x(1-p) of stake, so it costs 3.5% at the money and 1.1% at 84c.  An edge
    of the same size in probability points is worth very different amounts at
    different prices, and only this table says where it clears the fee.
    """
    cell = {}
    for t0, o, c, sig, pts in all_w:
        for tau, s in pts:
            if s <= 0 or o <= 0:
                continue
            p_up = PHI(math.log(s / o) / (sig * math.sqrt(tau)))
            fav_up = p_up >= 0.5
            p = p_up if fav_up else 1.0 - p_up
            y = 1.0 if ((c > o) == fav_up) else 0.0
            b = min(int((p - 0.5) * 10), 4)        # 5c bands from 0.50
            m = cell.setdefault(b, {}).setdefault(t0, [0, 0.0, 0.0])
            m[0] += 1
            m[1] += y - p
            m[2] += y - p - 0.07 * p * (1.0 - p)   # taker fee
    print("\n=== favourite by price, net of the 7%p(1-p) taker fee ===")
    print(f"  {'price':<12}{'obs':>9}{'windows':>9}{'too timid':>12}"
          f"{'net/sh':>10}{'t(net)':>9}")
    for b in sorted(cell):
        v = [x[1] / x[0] for x in cell[b].values()]
        nt = [x[2] / x[0] for x in cell[b].values()]
        sd = statistics.stdev(nt) if len(nt) > 1 else 0.0
        t = statistics.mean(nt) / (sd / math.sqrt(len(nt))) if sd else 0.0
        lbl = f"{0.5 + b*0.05:.2f}-{0.55 + b*0.05:.2f}" if b < 4 else "0.70+"
        print(f"  {lbl:<12}{sum(x[0] for x in cell[b].values()):>9,}"
              f"{len(v):>9,}{statistics.mean(v):>+12.4f}"
              f"{statistics.mean(nt):>+10.4f}{t:>+9.2f}")


if __name__ == "__main__":
    kl = json.load(open("klines30.json"))
    raw = {s: [[int(b[0]), float(b[1]), float(b[2])] for b in bars]
           for s, bars in kl.items()}
    # The hourly markets settle on close >= open of a 1-hour candle, the same
    # shape with a twelve-times-longer horizon.  Persistence is a property of
    # the price path, so it should be visible there too -- and if the hourly
    # BOOK prices it less well than the 5-minute book does, that is a predictor
    # a maker could lean on rather than only a spread to collect.
    if len(sys.argv) > 1 and sys.argv[1] == "hourly":
        globals()["WIN"], globals()["BARS"] = 3600, 60
        globals()["LOOK"] = 240
        sigma_free(raw)
        sys.exit()

    # Sigma sensitivity.  A trailing-vol estimate that runs high makes the model
    # price too close to 0.5, and the favourite then beats it for no reason but
    # the estimator -- which would look exactly like the result above.  Volatility
    # clusters, so a 60-bar window straddling a quiet patch over-states the next
    # five minutes.  If the effect is an artifact of that, shortening the window
    # to something that tracks the current regime should take it away.
    print("\n=== sigma sensitivity: trailing window vs the 0.55-0.65 edge ===")
    for lk in (10, 20, 60, 120, 240):
        globals()["LOOK"] = lk
        ws = [w for s, b in raw.items() for w in windows(b)]
        cell = {}
        for t0, o, c, sig, pts in ws:
            for tau, s in pts:
                if s <= 0 or o <= 0:
                    continue
                p_up = PHI(math.log(s / o) / (sig * math.sqrt(tau)))
                fav = p_up >= 0.5
                p = p_up if fav else 1.0 - p_up
                if not 0.55 <= p <= 0.65:
                    continue
                y = 1.0 if ((c > o) == fav) else 0.0
                m = cell.setdefault(t0, [0, 0.0])
                m[0] += 1; m[1] += y - p - 0.07 * p * (1.0 - p)
        v = [x[1] / x[0] for x in cell.values()]
        sd = statistics.stdev(v)
        print(f"  LOOK={lk:>4} bars   windows {len(v):>6,}   "
              f"net/sh {statistics.mean(v):+.4f}   "
              f"t {statistics.mean(v)/(sd/math.sqrt(len(v))):+.2f}")
    globals()["LOOK"] = 60
    # Cluster on the window timestamp, pooling the three assets: btc, eth and
    # sol move together, so one window is one observation, not three.
    all_w = []
    for sym, bars in kl.items():
        bars = [[int(b[0]), float(b[1]), float(b[2])] for b in bars]
        w = windows(bars)
        print(f"{sym}: {len(w):,} windows")
        all_w += w
    run(all_w, f"driftless random walk vs 30 days ({len(all_w):,} windows)")
    by_price(all_w)
    sigma_free(raw)

    # Placebo: same pipeline, window outcomes shuffled within asset-length
    # blocks.  Anything the method invents rather than measures shows up here.
    random.seed(7)
    closes = [w[2] for w in all_w]
    random.shuffle(closes)
    run(all_w, "placebo -- outcomes shuffled",
        flip={w[0]: c for w, c in zip(all_w, closes)})
