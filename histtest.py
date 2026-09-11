"""The decisive test: does the model beat the quoted price on hourly markets?

Everything measured live has been too small or contaminated. Hourly markets
make this answerable from history alone -- Polymarket serves quote history per
token, settlement is a public Binance candle, and both go back weeks. Scores the
model and the book against the same outcomes, on the same timestamps.
"""
import json, math, time, urllib.request, collections, statistics as st
import fair, run

UA = run.UA
HOUR = 3600


def get(u, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25) as r:
                return json.load(r)
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None


def main(days=22, assets=("btc", "eth", "sol")):
    bars = json.load(open("klines30.json"))
    tape = {}
    for sym, v in bars.items():
        rows = sorted((int(a), float(b), float(c)) for a, b, c in v)
        tape[sym] = (rows, {ts: i for i, (ts, _, _) in enumerate(rows)})

    now = int(time.time())
    h0 = (now // HOUR) * HOUR
    out = []
    for a in assets:
        sym = run.BN[a].upper()
        rows, idx = tape[sym]
        for k in range(2, 2 + days * 24):
            start = h0 - k * HOUR
            slug = run.hourly_slug(a, start)
            m = get(f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=true")
            if not m:
                continue
            m = m[0]
            pr = json.loads(m.get("outcomePrices") or "[]")
            if len(pr) != 2 or {pr[0], pr[1]} != {"1", "0"}:
                continue
            up_won = pr[0] == "1"
            tok = json.loads(m["clobTokenIds"])[0]
            hist = get(f"https://clob.polymarket.com/prices-history?market={tok}"
                       f"&startTs={start}&endTs={start + HOUR}&fidelity=1")
            if not hist:
                continue
            pts = hist.get("history", hist) if isinstance(hist, dict) else hist
            if start not in idx:
                continue
            K = rows[idx[start]][1]
            for p in pts:
                t = int(p["t"])
                if not (start < t < start + HOUR - 60):
                    continue
                i = idx.get(t // 60 * 60)
                if i is None or i < 60:
                    continue
                spot = rows[i][2]
                win = [rows[x][2] for x in range(i - 60, i)]
                d = [b - a2 for a2, b in zip(win, win[1:])]
                sig = st.pstdev(d) / math.sqrt(60.0)
                tau = start + HOUR - t - 60
                if tau <= 30 or sig <= 0:
                    continue
                out.append(dict(asset=a, hour=start, tau=tau,
                                model=fair.fair_up_naive(spot, K, tau, sig),
                                mkt=float(p["p"]), y=1.0 if up_won else 0.0))
            print(f"  {slug[-28:]:<28} pts={len(pts):>4} total={len(out):>6}", flush=True)
    json.dump(out, open("histtest22.json", "w"))
    print(f"\nDONE {len(out)} observations, {len(set((r['asset'], r['hour']) for r in out))} markets")


if __name__ == "__main__":
    main()


def analyse(path="histtest22.json"):
    """Score the BOOK on 22 days of hourly markets, not just the model.

    93k observations over 1,581 markets is the sample the live arms never had.
    Cluster on the hour: btc, eth and sol resolve the same hour of the same risk
    asset, so three markets per hour is one observation wearing three hats.
    """
    rows = json.load(open(path))
    print(f"\n=== {len(rows):,} observations, "
          f"{len({r['hour'] for r in rows}):,} hours ===")

    cal, fav = {}, {}
    for r in rows:
        m, y, h = r["mkt"], r["y"], r["hour"]
        if m is None or not 0.0 < m < 1.0:
            continue
        b = min(int(m * 10), 9)
        e = cal.setdefault(b, [0, 0.0, 0.0])
        e[0] += 1; e[1] += m; e[2] += y
        # the favourite and what it costs to take it
        p = m if m >= 0.5 else 1.0 - m
        w = y if m >= 0.5 else 1.0 - y
        k = min(int(round((p - 0.5) * 100)) // 5, 4)   # 5c bands
        # `mkt` is prices-history, which is a MID: checked directly by pulling
        # both tokens of one market and summing them -- median exactly 1.0000
        # over the paired timestamps, where two asks would sum to about 1.02.
        # So a taker does not get this price.  They cross the half-spread to buy
        # AND pay the fee; a maker is handed the half-spread and pays no fee.
        # HALF is the measured median hourly half-spread (section 46), applied as
        # a constant because prices-history carries no book.
        HALF = 0.010
        g = fav.setdefault(k, {}).setdefault(h, [0, 0.0, 0.0, 0.0])
        g[0] += 1
        g[1] += w - p
        g[2] += w - p - HALF - fair.taker_fee(p + HALF)
        g[3] += w - p + HALF

    print("\nbook calibration, hourly markets")
    print(f"  {'price':<10}{'n':>8}{'mean px':>10}{'won':>8}{'miss':>9}")
    for b in sorted(cal):
        n, sm, sw = cal[b]
        print(f"  {f'{b/10:.1f}-{b/10+0.1:.1f}':<10}{n:>8,}{sm/n:>10.3f}"
              f"{sw/n:>8.3f}{sw/n - sm/n:>+9.4f}")

    def tstat(v):
        sd = st.stdev(v) if len(v) > 1 else 0.0
        return st.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0

    print("\nfavourite vs the book's MID, clustered by hour")
    print(f"  {'price':<12}{'obs':>9}{'hours':>7}{'vs mid':>9}{'t':>7}"
          f"{'taker':>9}{'t':>7}{'maker':>9}{'t':>7}")
    for k in sorted(fav):
        per = fav[k]
        cols = [[x[i] / x[0] for x in per.values()] for i in (1, 2, 3)]
        lbl = f"{0.5+k*0.05:.2f}-{0.55+k*0.05:.2f}" if k < 4 else "0.70+"
        print(f"  {lbl:<12}{sum(x[0] for x in per.values()):>9,}{len(per):>7,}"
              + "".join(f"{st.mean(c):>+9.4f}{tstat(c):>+7.2f}" for c in cols))

    # One pre-specified test.  momentum.py predicted a hump over 0.55-0.65 from
    # 30 days of klines BEFORE this file was scored, so that band is not a
    # bucket chosen after the fact -- but it is still one test and gets reported
    # as one number, not as the best of five.
    pool = {}
    for k in (1, 2):
        for h, x in fav.get(k, {}).items():
            e = pool.setdefault(h, [0, 0.0, 0.0, 0.0])
            for i in range(4):
                e[i] += x[i]
    if pool:
        cols = [[x[i] / x[0] for x in pool.values()] for i in (1, 2, 3)]
        print(f"\n  0.55-0.65 pooled (predicted band), {len(pool)} hours:")
        for nm, c in zip(("vs mid", "taker", "maker"), cols):
            print(f"    {nm:<8}{st.mean(c):>+9.4f}   t={tstat(c):+.2f}")
