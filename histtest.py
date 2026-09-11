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


def main(days=6, assets=("btc", "eth", "sol")):
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
    json.dump(out, open("histtest.json", "w"))
    print(f"\nDONE {len(out)} observations, {len(set((r['asset'], r['hour']) for r in out))} markets")


if __name__ == "__main__":
    main()
