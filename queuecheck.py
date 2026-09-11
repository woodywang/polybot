"""Measure whether a resting order would actually get filled.

The maker case rests on one number nothing so far has measured: joining the
queue at the best bid, does the size ahead get consumed before the price moves
away? Two outcomes for a price level, and only one of them fills you:

  consumed  -- size at that price falls while the price stays; trades are
               happening and the queue is advancing toward you
  abandoned -- the best price moves; whatever was resting is now behind the
               market and fills only if the price comes back

Records, for each time the best bid takes a new price, how much size was
consumed at that level and how long it survived.
"""
import asyncio, json, time, statistics, urllib.request
from collections import defaultdict
import websockets

GAMMA = "https://gamma-api.polymarket.com/markets"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
WIN = 300


def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
        return json.load(r)


async def main(minutes=14):
    books, meta = {}, {}
    lvl = {}                      # token -> [price, peak_size, last_size, born]
    done = []
    stop = time.time() + minutes * 60

    def note(tok, price, size, now):
        cur = lvl.get(tok)
        if cur is None or abs(cur[0] - price) > 1e-9:
            if cur is not None:
                eaten = max(cur[1] - cur[2], 0.0)
                done.append(dict(tok=tok[:10], price=cur[0], peak=cur[1],
                                 left=cur[2], eaten=eaten, life=now - cur[3],
                                 cleared=cur[2] <= 1e-9))
            lvl[tok] = [price, size, size, now]
        else:
            cur[1] = max(cur[1], size)
            cur[2] = size

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
                                    p = max(bk)
                                    note(m["asset_id"], p, bk[p], time.time())
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
                                        p = max(bk)
                                        note(t, p, bk[p], time.time())
            except Exception:
                await asyncio.sleep(2)

    await asyncio.gather(refresh(), feed())
    print(f"\n=== {len(done)} best-bid levels observed to the end of their life ===")
    if len(done) < 20:
        return
    cleared = [d for d in done if d["cleared"]]
    print(f"  queue fully cleared (a joiner would have filled): {len(cleared)} "
          f"({len(cleared)/len(done)*100:.0f}%)")
    print(f"  level lifetime      median {statistics.median([d['life'] for d in done]):.1f}s")
    print(f"  size at the level   median {statistics.median([d['peak'] for d in done]):.0f} sh")
    eaten = [d["eaten"] / d["peak"] for d in done if d["peak"] > 0]
    print(f"  fraction consumed   median {statistics.median(eaten)*100:.0f}%  "
          f"mean {statistics.mean(eaten)*100:.0f}%")
    json.dump(done, open("queuecheck.json", "w"))


if __name__ == "__main__":
    asyncio.run(main())
