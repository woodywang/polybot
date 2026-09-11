"""Is the 200ms lag the book's, or mine?

Section 82 cross-correlated arrival times: both feeds land in one event loop, so
any delay in draining the Polymarket socket shows up as the book reacting
slowly. Given that this project spent the day discovering the Polymarket feed
outruns its own consumer (section 60), that is not a hypothetical.

Both protocols carry an exchange timestamp -- Binance `@trade` has `T`,
Polymarket `price_change` has `timestamp` -- so arrival minus exchange time is
this process's own delay per feed, measured separately. If Polymarket's messages
sit ~200ms longer than Binance's before being processed, section 82 measured
the harness and not the market.
"""
import asyncio, json, time, statistics as st, urllib.request, sys
import websockets
import run

UA = run.UA


def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
        return json.load(r)


async def main(minutes=12, asset="btc"):
    bn, pm = [], []
    stop = time.time() + minutes * 60

    async def binance():
        url = f"wss://stream.binance.com:9443/ws/{run.BN[asset]}@trade"
        while time.time() < stop:
            try:
                async with websockets.connect(url, ping_interval=None,
                                              max_queue=None) as ws:
                    while time.time() < stop:
                        d = json.loads(await asyncio.wait_for(ws.recv(), 20))
                        bn.append(time.time() - float(d["T"]) / 1000.0)
            except Exception:
                await asyncio.sleep(2)

    async def poly():
        toks = []
        b = int(time.time() // 300) * 300
        for k in (0, 1):
            try:
                m = get(f"https://gamma-api.polymarket.com/markets"
                        f"?slug={asset}-updown-5m-{b + k*300}&closed=false")
                if m:
                    toks += json.loads(m[0]["clobTokenIds"])
            except Exception:
                pass
        if not toks:
            return
        while time.time() < stop:
            try:
                async with websockets.connect(run.POLY_WS, ping_interval=None,
                                              max_queue=None) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    while time.time() < stop:
                        raw = await asyncio.wait_for(ws.recv(), 25)
                        if raw == "PONG":
                            continue
                        now = time.time()
                        j = json.loads(raw)
                        for m in (j if isinstance(j, list) else [j]):
                            ts = m.get("timestamp")
                            if ts:
                                pm.append(now - float(ts) / 1000.0)
            except Exception:
                await asyncio.sleep(2)

    await asyncio.gather(binance(), poly())

    def show(v, lab):
        if len(v) < 50:
            return print(f"  {lab}: only {len(v)} samples")
        v = sorted(v)
        print(f"  {lab:<12} n={len(v):>6,}  median {v[len(v)//2]*1000:8.1f}ms"
              f"  p90 {v[int(len(v)*.9)]*1000:8.1f}ms"
              f"  p99 {v[int(len(v)*.99)]*1000:9.1f}ms")
    print(f"\narrival minus exchange timestamp ({minutes} min, {asset})")
    show(bn, "binance")
    show(pm, "polymarket")
    if len(bn) > 50 and len(pm) > 50:
        d = st.median(sorted(pm)) - st.median(sorted(bn))
        print(f"\n  polymarket is processed {d*1000:+.1f}ms later than binance")
        print(f"  section 82 measured a 200ms book lag; {abs(d)*1000:.0f}ms of "
              f"that is this process"
              f"{' -- ALL OF IT' if abs(d) > 0.15 else ''}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 12))
