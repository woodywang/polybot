"""Decide whether a stale websocket book is a frozen market or a broken feed.

The whole project's apparent edge sits in quotes that had stopped moving. Those
are either real resting orders nobody is taking -- in which case the edge is
reachable -- or a feed that quietly stopped for that token, in which case it
never existed. Paper trading cannot tell the two apart, but the CLOB REST book
can: poll it for a token the websocket says is frozen and compare.
"""
import asyncio, json, time, urllib.request, statistics
from collections import defaultdict
import websockets

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/book?token_id="
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
WIN, STALE = 300, 20.0


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
        return json.load(r)


def best(book):
    bids = [(float(x["price"]), float(x["size"])) for x in book.get("bids", []) if float(x["size"]) > 0]
    asks = [(float(x["price"]), float(x["size"])) for x in book.get("asks", []) if float(x["size"]) > 0]
    return (max(bids)[0] if bids else None, min(asks)[0] if asks else None,
            min(asks)[1] if asks else 0.0)


async def main(minutes=12):
    books, touched, meta = {}, {}, {}
    stop = time.time() + minutes * 60
    results = []

    async def refresh():
        while time.time() < stop:
            b = int(time.time() // WIN) * WIN
            slugs = [f"{a}-updown-5m-{b + k * WIN}" for a in ("btc", "eth", "sol")
                     for k in (0, 1)]
            try:
                q = "&".join(f"slug={s}" for s in slugs) + "&closed=false"
                for m in get(f"{GAMMA}?{q}"):
                    for i, t in enumerate(json.loads(m["clobTokenIds"])):
                        meta[t] = (m["slug"], "Up" if i == 0 else "Down")
            except Exception:
                pass
            await asyncio.sleep(30)

    async def feed():
        while time.time() < stop:
            toks = list(meta)
            if not toks:
                await asyncio.sleep(2); continue
            try:
                # max_queue=None, as in run.py.  The first run of this
                # diagnostic used the library default and reported the websocket
                # book disagreeing with REST 27% of the time -- which section 60
                # showed is what a socket being closed for slow consumption
                # looks like.  Rerun with the backpressure removed: if the
                # disagreements were my own drops, they go away.
                async with websockets.connect(WS, ping_interval=None,
                                              max_queue=None) as ws:
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
                                books[m["asset_id"]] = m
                                touched[m["asset_id"]] = time.time()
                            elif et == "price_change":
                                for c in m.get("price_changes", []):
                                    if c.get("asset_id"):
                                        touched[c["asset_id"]] = time.time()
                                        bk = books.setdefault(c["asset_id"], {"bids": [], "asks": []})
                                        side = "bids" if c.get("side") == "BUY" else "asks"
                                        lv = [x for x in bk.get(side, [])
                                              if abs(float(x["price"]) - float(c["price"])) > 1e-9]
                                        if float(c["size"]) > 0:
                                            lv.append({"price": c["price"], "size": c["size"]})
                                        bk[side] = lv
            except Exception:
                await asyncio.sleep(2)

    async def probe():
        seen = set()
        while time.time() < stop:
            await asyncio.sleep(5)
            now = time.time()
            for t, ts in list(touched.items()):
                age = now - ts
                if age < STALE or (t, int(age // 20)) in seen:
                    continue
                seen.add((t, int(age // 20)))
                try:
                    rest = await asyncio.to_thread(get, CLOB + t)
                except Exception:
                    continue
                wb, wa, wsz = best(books.get(t, {}))
                rb, ra, rsz = best(rest)
                same = (wb == rb and wa == ra)
                results.append(dict(tok=t[:12], slug=meta.get(t, ("?",))[0][-14:],
                                    age=round(age, 1), ws=(wb, wa), rest=(rb, ra),
                                    same=same, rest_sz=rsz))
                print(f"  stale {age:5.1f}s  {meta.get(t,('?',''))[0][-14:]:<14} "
                      f"WS {str(wb):>6}/{str(wa):<6} REST {str(rb):>6}/{str(ra):<6} "
                      f"{'SAME  (frozen market)' if same else 'DIFFERENT (dead feed)'}", flush=True)

    await asyncio.gather(refresh(), feed(), probe())
    print(f"\n=== {len(results)} stale books cross-checked ===")
    if results:
        same = sum(1 for r in results if r["same"])
        print(f"  identical to REST : {same}  ({same/len(results)*100:.0f}%)  -> order really is resting")
        print(f"  different         : {len(results)-same}  -> websocket had gone quiet")
        json.dump(results, open("stalecheck.json", "w"), indent=1)


if __name__ == "__main__":
    asyncio.run(main())
