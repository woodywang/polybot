"""Negative control: run the hourly pipeline unchanged on 5-minute markets.

If the hourly edge is real, the identical test here should find little or
nothing -- every route into these markets has already closed. If it finds a
comparable edge, the methodology is what is generating it and the hourly result
cannot be trusted.
"""
import json, math, time, urllib.request, statistics as st
import fair, run

UA, WIN = run.UA, 300


def get(u, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=25) as r:
                return json.load(r)
        except Exception:
            time.sleep(1.2 * (i + 1))
    return None


def main(hours=30, assets=("btc", "eth", "sol")):
    bars = json.load(open("klines30.json"))
    tape = {}
    for sym, v in bars.items():
        rows = sorted((int(a), float(b), float(c)) for a, b, c in v)
        tape[sym] = (rows, {ts: i for i, (ts, _, _) in enumerate(rows)})

    now = int(time.time())
    w0 = (now // WIN) * WIN
    out = []
    for a in assets:
        rows, idx = tape[run.BN[a].upper()]
        for k in range(3, 3 + hours * 12):
            start = w0 - k * WIN
            slug = f"{a}-updown-5m-{start}"
            m = get(f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=true")
            if not m:
                continue
            m = m[0]
            pr = json.loads(m.get("outcomePrices") or "[]")
            if len(pr) != 2 or {pr[0], pr[1]} != {"1", "0"}:
                continue
            up_won = pr[0] == "1"
            tok = json.loads(m["clobTokenIds"])[0]
            h = get(f"https://clob.polymarket.com/prices-history?market={tok}"
                    f"&startTs={start}&endTs={start + WIN}&fidelity=1")
            if not h:
                continue
            pts = h.get("history", h) if isinstance(h, dict) else h
            # strike: the 60s mean before the window, as the live harness uses
            i0 = idx.get(start)
            if i0 is None or i0 < 61:
                continue
            K = st.mean([rows[x][2] for x in range(i0 - 1, i0)]) if False else rows[i0][1]
            for p in pts:
                t = int(p["t"])
                if not (start < t < start + WIN - 30):
                    continue
                i = idx.get(t // 60 * 60)
                if i is None or i < 60:
                    continue
                spot = rows[i][2]
                win = [rows[x][2] for x in range(i - 60, i)]
                d = [b - a2 for a2, b in zip(win, win[1:])]
                sig = st.pstdev(d) / math.sqrt(60.0)
                tau = start + WIN - t
                if tau <= 20 or sig <= 0:
                    continue
                out.append(dict(asset=a, win=start, tau=tau,
                                model=fair.fair_up_naive(spot, K, tau, sig),
                                mkt=float(p["p"]), y=1.0 if up_won else 0.0))
            if len(out) % 500 < 60:
                print(f"  {slug:<28} total={len(out):>6}", flush=True)
    json.dump(out, open("ctrl5m.json", "w"))
    print(f"\nDONE {len(out)} obs, {len(set((r['asset'], r['win']) for r in out))} markets")


if __name__ == "__main__":
    main()
