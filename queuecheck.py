"""Would a resting order fill? Third attempt, and the first with the right clock.

Versions one and two both ended a level when the BEST BID PRICE changed, which
is not what happens to an order. Somebody bidding higher does not cancel your
order at 0.50 -- it just stops being the best bid, and it still fills if the
price comes back. Measuring only while your price is the top of book gave level
lifetimes of 0.04s and a fill rate near zero, both artifacts of the clock.

This posts at the prevailing best bid, then holds for a fixed window and counts
every sell that trades at or below that price. A joiner queued behind the
resting size fills when that cumulative volume exceeds it.
"""
import asyncio, json, time, statistics, urllib.request
from collections import defaultdict
import websockets

GAMMA = "https://gamma-api.polymarket.com/markets"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
WIN, HOLD = 300, 60.0


def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
        return json.load(r)


async def main(minutes=16):
    books, meta, best = {}, {}, {}
    orders = []                       # live simulated posts
    done = []
    stop = time.time() + minutes * 60

    async def refresh():
        while time.time() < stop:
            b = int(time.time() // WIN) * WIN
            slugs = [f"{a}-updown-5m-{b + k * WIN}" for a in ("btc", "eth", "sol") for k in (0, 1)]
            try:
                for m in get(f"{GAMMA}?" + "&".join(f"slug={s}" for s in slugs) + "&closed=false"):
                    for t in json.loads(m["clobTokenIds"]):
                        meta[t] = m["slug"]
            except Exception:
                pass
            await asyncio.sleep(30)

    async def poster():
        """Post one order per token every HOLD seconds, at the prevailing bid."""
        while time.time() < stop:
            now = time.time()
            for t, b in list(best.items()):
                if any(o["tok"] == t and not o["closed"] for o in orders):
                    continue
                p, sz = b
                if p is None or sz <= 0:
                    continue
                orders.append(dict(tok=t, price=p, ahead=sz, sold=0.0,
                                   born=now, closed=False))
            for o in orders:
                if not o["closed"] and now - o["born"] >= HOLD:
                    o["closed"] = True
                    done.append(dict(price=o["price"], ahead=o["ahead"],
                                     sold=o["sold"], filled=o["sold"] >= o["ahead"]))
            await asyncio.sleep(1)

    async def feed():
        while time.time() < stop:
            toks = list(meta)
            if not toks:
                await asyncio.sleep(2); continue
            try:
                async with websockets.connect(WS, ping_interval=None) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    n0 = len(toks)
                    while time.time() < stop and len(meta) == n0:
                        raw = await asyncio.wait_for(ws.recv(), timeout=25)
                        if raw == "PONG":
                            continue
                        j = json.loads(raw)
                        for m in (j if isinstance(j, list) else [j]):
                            et = m.get("event_type")
                            if et == "book" and m.get("asset_id"):
                                bk = {float(x["price"]): float(x["size"])
                                      for x in m.get("bids", []) if float(x["size"]) > 0}
                                books[m["asset_id"]] = bk
                                if bk:
                                    p = max(bk); best[m["asset_id"]] = (p, bk[p])
                            elif et == "price_change":
                                for c in m.get("price_changes", []):
                                    t = c.get("asset_id")
                                    if not t or c.get("side") != "BUY":
                                        continue
                                    bk = books.setdefault(t, {})
                                    px, sz = float(c["price"]), float(c["size"])
                                    if sz <= 0:
                                        bk.pop(px, None)
                                    else:
                                        bk[px] = sz
                                    if bk:
                                        p = max(bk); best[t] = (p, bk[p])
                            elif et == "last_trade_price" and m.get("side") == "SELL":
                                t = m.get("asset_id"); px = float(m["price"])
                                for o in orders:
                                    if (not o["closed"] and o["tok"] == t
                                            and px <= o["price"] + 1e-9):
                                        o["sold"] += float(m["size"])
            except Exception:
                await asyncio.sleep(2)

    await asyncio.gather(refresh(), poster(), feed())
    print(f"\n=== {len(done)} simulated posts held {HOLD:.0f}s each ===")
    if len(done) < 30:
        return
    f = [d for d in done if d["filled"]]
    print(f"  queue ahead cleared within {HOLD:.0f}s: {len(f)} ({len(f)/len(done)*100:.1f}%)")
    print(f"  size ahead at post   median {statistics.median([d['ahead'] for d in done]):.0f} sh")
    print(f"  sell volume in {HOLD:.0f}s  median {statistics.median([d['sold'] for d in done]):.0f} sh"
          f"   mean {statistics.mean([d['sold'] for d in done]):.0f} sh")
    r = [d["sold"] / d["ahead"] for d in done if d["ahead"] > 0]
    r.sort()
    print(f"  sold / ahead         median {statistics.median(r):.2f}"
          f"   p75 {r[int(len(r)*.75)]:.2f}   p90 {r[int(len(r)*.9)]:.2f}")
    json.dump(done, open("queuecheck.json", "w"))


if __name__ == "__main__":
    asyncio.run(main())
