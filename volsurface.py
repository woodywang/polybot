"""Does the BTC strike ladder's implied volatility beat subsequent realised?

Section 96: a 5-minute crypto digital demands a forecast ten times finer than
its own settlement reference is defined, which is why nothing in this project
worked. Section 97 found the instrument where that arithmetic is not hopeless --
`btc-multi-strikes-weekly`, daily events with a full strike ladder, settling on
the Binance 1-minute candle at noon ET, which section 87 verified at 0.00%
disagreement.

One ladder at one instant proves nothing (section 93's mistake). This walks the
whole series: for each past day, recover the implied lognormal from the ladder at
a fixed lead time, then measure what volatility actually turned up between then
and settlement.

Writes volsurface.json.
"""
import json, math, time, urllib.request, statistics as st, sys

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
YR = 365 * 24 * 3600


def g(u, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None


def Phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def iPhi(p):
    lo, hi = -8.0, 8.0
    for _ in range(80):
        m = (lo + hi) / 2
        if Phi(m) < p:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def klines(start_ms, end_ms):
    """1-minute closes over [start, end]."""
    out = []
    t = start_ms
    while t < end_ms:
        b = g(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
              f"&startTime={t}&endTime={end_ms}&limit=1000")
        if not b:
            break
        out += [(int(x[0]) // 1000, float(x[4])) for x in b]
        if len(b) < 1000:
            break
        t = int(b[-1][0]) + 60_000
    return out


def fit_ladder(pairs):
    """pairs: [(strike, prob_above)] -> (median, sigma_over_tau) by least squares.

    Two usable strikes define it exactly; more are fitted through the pair that
    straddles the median most tightly, which is where the ladder carries the most
    information and the least tick noise.
    """
    us = [(k, p) for k, p in pairs if 0.02 < p < 0.98]
    if len(us) < 2:
        return None
    us.sort(key=lambda x: abs(x[1] - 0.5))
    (k1, p1), (k2, p2) = us[0], us[1]
    if abs(k1 - k2) < 1e-9:
        return None
    z1, z2 = iPhi(p1), iPhi(p2)
    if abs(z1 - z2) < 1e-9:
        return None
    sig = (math.log(k2) - math.log(k1)) / (z1 - z2)
    if sig <= 0:
        return None
    mu = math.log(k1) + z1 * sig
    return math.exp(mu), sig


def main(lead_h=16.0, limit=20):
    evs = g(f"{GAMMA}/events?series_id=45&limit={limit}&closed=true"
            f"&order=endDate&ascending=false") or []
    print(f"{len(evs)} closed events in btc-multi-strikes-weekly\n")
    rows = []
    cal = {}
    for e in evs:
        mk = e.get("markets") or []
        end = e.get("endDate")
        if not end or not mk:
            continue
        t_end = int(time.mktime(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")))
        t_obs = t_end - int(lead_h * 3600)
        pairs = []
        settle = None
        for m in mk:
            s = m.get("slug", "")
            try:
                k = float(s.split("bitcoin-above-")[1].split("k-")[0]) * 1000
            except Exception:
                continue
            toks = json.loads(m.get("clobTokenIds") or "[]")
            if not toks:
                continue
            h = g(f"{CLOB}/prices-history?market={toks[0]}"
                  f"&startTs={t_obs - 900}&endTs={t_obs + 900}&fidelity=1")
            pts = (h or {}).get("history") or []
            if not pts:
                continue
            p = min(pts, key=lambda x: abs(int(x["t"]) - t_obs))
            pairs.append((k, float(p["p"])))
        # what actually happened: the noon-ET close
        kl_end = klines((t_end - 300) * 1000, (t_end + 60) * 1000)
        settle = kl_end[-1][1] if kl_end else None
        if settle:
            for k, p in pairs:
                b = min(int(p * 10), 9)
                cal.setdefault(b, []).append((end[:10], p, 1.0 if settle > k else 0.0))
        fit = fit_ladder(pairs)
        if not fit:
            print(f"  {end[:10]}  {len(pairs)} strikes, no usable pair")
            continue
        med, sig = fit
        iv = sig / math.sqrt(lead_h * 3600 / YR)
        kl = klines((t_obs - 60) * 1000, t_end * 1000)
        if len(kl) < 60:
            print(f"  {end[:10]}  no klines")
            continue
        r = [math.log(b[1] / a[1]) for a, b in zip(kl, kl[1:]) if a[1] > 0]
        rv = st.pstdev(r) * math.sqrt(YR / 60.0)
        spot0 = kl[0][1]
        rows.append(dict(day=end[:10], strikes=len(pairs), median=med, spot=spot0,
                         iv=iv, rv=rv, ratio=iv / rv if rv else None))
        print(f"  {end[:10]}  {len(pairs):>2} strikes  spot {spot0:>9,.0f}"
              f"  implied median {med:>9,.0f}  IV {iv*100:>5.1f}%"
              f"  RV {rv*100:>5.1f}%  IV/RV {iv/rv:>5.2f}")
    # --- is the LADDER calibrated, not just its centre?
    # Section 98 fits the two strikes straddling the median and finds the
    # implied vol unbiased.  The wings are a different claim: prediction markets
    # classically overprice longshots, and a smile that is too steep would show
    # up as far strikes settling in-the-money less often than they were priced.
    # Strikes inside one day share a single price path, so the effective sample
    # is the DAY count, not the strike count -- scored per day, then pooled.
    if cal:
        print("\n  ladder calibration: implied probability vs what happened")
        print(f"    {'implied':<12}{'obs':>6}{'days':>6}{'mean p':>9}{'hit':>8}{'miss':>9}")
        for b in sorted(cal):
            v = cal[b]
            per = {}
            for d, p, y in v:
                e = per.setdefault(d, [0, 0.0, 0.0])
                e[0] += 1; e[1] += p; e[2] += y
            mp = st.mean([x[1] / x[0] for x in per.values()])
            hy = st.mean([x[2] / x[0] for x in per.values()])
            lbl = f"{b/10:.1f}-{b/10+0.1:.1f}"
            print(f"    {lbl:<12}{len(v):>6}{len(per):>6}{mp:>9.3f}{hy:>8.3f}"
                  f"{hy-mp:>+9.3f}")

    if len(rows) >= 4:
        ra = [r["ratio"] for r in rows if r["ratio"]]
        ivs = [r["iv"] for r in rows]
        rvs = [r["rv"] for r in rows]
        print(f"\n  {len(ra)} days")
        print(f"  mean IV {st.mean(ivs)*100:.1f}%   mean RV {st.mean(rvs)*100:.1f}%")
        print(f"  IV/RV  mean {st.mean(ra):.3f}  median {st.median(ra):.3f}"
              f"  sd {st.pstdev(ra):.3f}")
        d = [r["iv"] - r["rv"] for r in rows]
        sd = st.stdev(d) if len(d) > 1 else 0.0
        t = st.mean(d) / (sd / math.sqrt(len(d))) if sd else 0.0
        print(f"  IV - RV  mean {st.mean(d)*100:+.1f} points  t={t:+.2f}")
        print(f"\n  IV/RV < 1 means the book underprices volatility -- buy the wings.")
    json.dump(rows, open("volsurface.json", "w"))


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 16.0)
