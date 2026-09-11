"""How long does Polymarket take to reprice after Binance moves?

Section 69 found no stale quotes: the maximum age across 304 samples was 1.8
seconds, measured by sampling every 20s. That closes staleness at the scale the
guard operates on (--max-stale 20) and says nothing at all about the scale the
remaining hypothesis lives at.

The account in section 1 clears 1.64c a share as a taker where the displayed
quote returns -1.70c. Every explanation this project can test is now negative
except one: that it is faster than the book. If Polymarket's mid follows
Binance's with a lag of L milliseconds, then for L milliseconds after every
Binance move the quote is stale in a *predictable direction* -- which is a
taker edge invisible to any instrument sampling at 20 seconds.

Cross-correlates Binance mid returns against Polymarket mid returns at lags from
0 to 5s, on 100ms bars. The lag that maximises correlation is the book's
reaction time. Writes latency.json.
"""
import asyncio, json, math, time, urllib.request, statistics as st, sys
import websockets
import run

UA = run.UA
BAR = 0.1                      # 100ms bars
MAXLAG = 50                    # bars, so 5 seconds


def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
        return json.load(r)


async def main(minutes=20, asset="btc"):
    bn, pm, meta = {}, {}, {}
    stop = time.time() + minutes * 60

    def bar(t):
        return int(t / BAR)

    async def binance():
        url = f"wss://stream.binance.com:9443/ws/{run.BN[asset]}@bookTicker"
        while time.time() < stop:
            try:
                async with websockets.connect(url, ping_interval=None,
                                              max_queue=None) as ws:
                    while time.time() < stop:
                        d = json.loads(await asyncio.wait_for(ws.recv(), 20))
                        mid = (float(d["b"]) + float(d["a"])) / 2.0
                        bn[bar(time.time())] = mid
            except Exception:
                await asyncio.sleep(2)

    async def markets():
        while time.time() < stop:
            b = int(time.time() // 300) * 300
            for k in (0, 1):
                slug = f"{asset}-updown-5m-{b + k * 300}"
                if slug in meta:
                    continue
                try:
                    m = await asyncio.to_thread(
                        get, f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=false")
                except Exception:
                    continue
                if m:
                    t = json.loads(m[0]["clobTokenIds"])
                    meta[slug] = dict(up=t[0], dn=t[1], start=b + k * 300)
            await asyncio.sleep(60)

    async def poly():
        books = {}
        while time.time() < stop:
            toks = [t for m in meta.values() for t in (m["up"], m["dn"])]
            if not toks:
                await asyncio.sleep(3); continue
            try:
                async with websockets.connect(
                        run.POLY_WS, ping_interval=None, max_queue=None) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    n0 = len(toks)
                    while time.time() < stop and len(meta) * 2 == n0:
                        raw = await asyncio.wait_for(ws.recv(), 25)
                        if raw == "PONG":
                            continue
                        j = json.loads(raw)
                        for m in (j if isinstance(j, list) else [j]):
                            et = m.get("event_type")
                            if et == "book" and m.get("asset_id"):
                                books.setdefault(m["asset_id"], run.Book()).snapshot(m)
                            elif et == "price_change":
                                for c in m.get("price_changes", []):
                                    if c.get("asset_id"):
                                        books.setdefault(c["asset_id"], run.Book()).level(c)
                            else:
                                continue
                        # the CURRENT window only: it is the one being repriced
                        now = time.time()
                        cur = [v for v in meta.values()
                               if 0 < v["start"] + 300 - now < 300]
                        if not cur:
                            continue
                        mk = cur[0]
                        bu, bd = books.get(mk["up"]), books.get(mk["dn"])
                        if not bu or not bd:
                            continue
                        au, _ = bu.best_ask(); ad, _ = bd.best_ask()
                        if au and ad:
                            pm[bar(now)] = (au + 1.0 - ad) / 2.0
            except Exception:
                await asyncio.sleep(2)

    await asyncio.gather(binance(), markets(), poly())

    # forward-fill both series onto a common bar grid, then cross-correlate
    ks = sorted(set(bn) | set(pm))
    if len(ks) < 500:
        return print(f"only {len(ks)} bars, not enough")
    b_last = p_last = None
    B, P = [], []
    for k in range(ks[0], ks[-1] + 1):
        b_last = bn.get(k, b_last); p_last = pm.get(k, p_last)
        if b_last is not None and p_last is not None:
            B.append(b_last); P.append(p_last)
    db = [math.log(b / a) for a, b in zip(B, B[1:]) if a > 0]
    dp = [b - a for a, b in zip(P, P[1:])]
    print(f"\n{len(db):,} bars of {BAR*1000:.0f}ms "
          f"({len(db)*BAR/60:.1f} min of overlap)")

    def corr(x, y):
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        mx, my = st.mean(x), st.mean(y)
        num = sum((a - mx) * (b - my) for a, b in zip(x, y))
        dx = math.sqrt(sum((a - mx) ** 2 for a in x))
        dy = math.sqrt(sum((b - my) ** 2 for b in y))
        return num / (dx * dy) if dx and dy else 0.0

    print(f"  {'lag':>7}{'corr(binance_t, poly_t+lag)':>30}")
    best = (0, -9)
    rows = []
    for lag in range(0, MAXLAG + 1):
        c = corr(db[:len(db) - lag], dp[lag:])
        rows.append((lag * BAR, c))
        if c > best[1]:
            best = (lag * BAR, c)
        if lag <= 20 or lag % 5 == 0:
            print(f"  {lag*BAR*1000:>5.0f}ms{c:>30.4f}")
    print(f"\n  peak correlation {best[1]:.4f} at lag {best[0]*1000:.0f}ms")
    json.dump(dict(rows=rows, peak=best), open("latency.json", "w"))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20))
