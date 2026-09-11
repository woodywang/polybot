"""Would a resting order actually fill? Measured against trades, not the book.

The first version of this tracked the best-bid price level and called it dead
whenever the best bid moved -- which counts "somebody bid higher" as "your order
died", and reported a 0% fill rate that was mostly that bug.

This one measures the thing that actually fills an order. A maker resting on the
bid is filled when a taker SELLS into it, so for each best-bid level we track
the resting size a joiner would queue behind and the cumulative sell-side volume
that trades at or below that price while the level is alive. The joiner fills
when that volume exceeds the size ahead.
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
    lvl = {}                 # token -> dict(price, ahead, sold, born)
    done = []
    stop = time.time() + minutes * 60

    def roll(tok, price, size, now):
        cur = lvl.get(tok)
        if cur is not None and abs(cur["price"] - price) < 1e-9:
            cur["ahead"] = min(cur["ahead"], size)   # queue can only shrink ahead of us
            return
        if cur is not None:
            done.append(dict(price=cur["price"], ahead0=cur["ahead0"],
                             sold=cur["sold"], life=now - cur["born"],
                             filled=cur["sold"] >= cur["ahead0"]))
        lvl[tok] = dict(price=price, ahead=size, ahead0=size, sold=0.0, born=now)

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
                            now = time.time()
                            if et == "book" and m.get("asset_id"):
                                bk = {float(x["price"]): float(x["size"])
                                      for x in m.get("bids", []) if float(x["size"]) > 0}
                                books[m["asset_id"]] = bk
                                if bk:
                                    p = max(bk); roll(m["asset_id"], p, bk[p], now)
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
                                        p = max(bk); roll(t, p, bk[p], now)
                            elif et == "last_trade_price":
                                t = m.get("asset_id")
                                cur = lvl.get(t)
                                # a maker on the bid fills when a taker sells
                                if cur and m.get("side") == "SELL":
                                    if float(m["price"]) <= cur["price"] + 1e-9:
                                        cur["sold"] += float(m["size"])
            except Exception:
                await asyncio.sleep(2)

    await asyncio.gather(refresh(), feed())
    print(f"\n=== {len(done)} best-bid levels tracked to their end ===")
    if len(done) < 30:
        return
    filled = [d for d in done if d["filled"]]
    print(f"  a joiner behind the whole queue would have filled: "
          f"{len(filled)} ({len(filled)/len(done)*100:.1f}%)")
    print(f"  size ahead at join   median {statistics.median([d['ahead0'] for d in done]):.0f} sh")
    print(f"  sell volume at level median {statistics.median([d['sold'] for d in done]):.0f} sh"
          f"   mean {statistics.mean([d['sold'] for d in done]):.0f} sh")
    print(f"  level lifetime       median {statistics.median([d['life'] for d in done]):.2f}s")
    ratio = [d["sold"] / d["ahead0"] for d in done if d["ahead0"] > 0]
    print(f"  sold / size-ahead    median {statistics.median(ratio):.3f}"
          f"   90th pct {sorted(ratio)[int(len(ratio)*0.9)]:.3f}")
    json.dump(done, open("queuecheck.json", "w"))


if __name__ == "__main__":
    asyncio.run(main())
