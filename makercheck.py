"""Does a resting bid make money on the HOURLY markets?

Section 41 measured the model's residual edge over the book at nil, which makes
the 7%x(1-p) taker fee the whole story and posting the only route left.  Filling
is the easy half of that question and the wrong one to stop at: a bid that fills
is a bid somebody chose to hit, so the fills arrive exactly when the price is
about to go against you.  Fill rate without markout is how naive market making
looks profitable right up until it is funded.

So this posts at the prevailing best bid on every open hourly token, waits for
the cumulative sell volume at or below that price to clear the size already
queued ahead, and then records where the mid sits 30s and 120s later.  Markout
is in cents per share and is what the maker actually earns before any rebate.
"""
import asyncio, json, time, statistics, urllib.request, sys
import websockets

GAMMA = "https://gamma-api.polymarket.com/markets"
WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
ASSETS = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana"}
MARKOUTS = (30.0, 120.0)
# Section 68: the spread is quoted at about twice the 30s mid travel, so a
# one-tick improvement only pays where the spread is 3c or wider.  IMPROVE posts
# a tick above the touch and MINCAP refuses to post at all unless (mid - price)
# still clears this, which is the configuration section 67 opened up.  With
# IMPROVE=0 and MINCAP=-1 this is the original at-the-touch measurement.
# MK_5MIN: the 5-minute crypto markets carry makerRebatesFeeShareBps = 10000
# and the hourly ones do not, which inverts the instrument comparison every
# maker test so far was built on.  Making was never measured here because
# section 46 showed a 0.5c half-spread against 2.5c of 10s travel; with a
# 100% fee-share rebate the gross is ~2.25c at the money, not 0.5c.
FIVE = __import__("os").environ.get("MK_5MIN", "") == "1"
IMPROVE = int(__import__("os").environ.get("MK_IMPROVE", "0"))
MINCAP = float(__import__("os").environ.get("MK_MINCAP", "-1"))
MAXWAIT = 600.0                      # give a post ten minutes to fill


def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
        return json.load(r)


def hourly_slug(asset, start_utc):
    t = time.gmtime(start_utc - 4 * 3600)          # ET = UTC-4
    h = t.tm_hour % 12 or 12
    ap = "am" if t.tm_hour < 12 else "pm"
    mon = time.strftime("%B", t).lower()
    return f"{asset}-up-or-down-{mon}-{t.tm_mday}-{t.tm_year}-{h}{ap}-et"


async def main(minutes=60):
    books, asks, meta, best = {}, {}, {}, {}
    live, done = [], []
    stop = time.time() + minutes * 60

    def mid(t):
        b, a = books.get(t) or {}, asks.get(t) or {}
        if not b or not a:
            return None
        return (max(b) + min(a)) / 2

    async def refresh():
        while time.time() < stop:
            if FIVE:
                base = int(time.time() // 300) * 300
                slugs = [f"{a}-updown-5m-{base + k * 300}"
                         for a in ASSETS for k in (0, 1)]
                for s in slugs:
                    try:
                        for m in get(f"{GAMMA}?slug={s}&closed=false"):
                            for t in json.loads(m["clobTokenIds"]):
                                meta[t] = m["slug"]
                    except Exception:
                        pass
                await asyncio.sleep(30)
                continue
            base = int(time.time() // 3600) * 3600
            slugs = [hourly_slug(a, base + k * 3600)
                     for a in ASSETS.values() for k in (0, 1)]
            for s in slugs:                      # one slug per request: gamma
                try:                             # returns [] for batched slugs
                    for m in get(f"{GAMMA}?slug={s}&closed=false"):
                        for t in json.loads(m["clobTokenIds"]):
                            meta[t] = m["slug"]
                except Exception:
                    pass
            await asyncio.sleep(120)

    async def poster():
        while time.time() < stop:
            now = time.time()
            for t, (p, sz) in list(best.items()):
                if p is None or sz <= 0 or p < 0.02 or p > 0.98:
                    continue
                if any(o["tok"] == t and o["state"] == "queued" for o in live):
                    continue
                m = mid(t)
                px, ahead = p, sz
                if IMPROVE:
                    px, ahead = round(p + 0.01 * IMPROVE, 4), 0.0
                if m is None or m - px < MINCAP:
                    continue
                live.append(dict(tok=t, price=px, ahead=ahead, sold=0.0,
                                 capture=m - px, spread=None,
                                 born=now, state="queued", fill_ts=None,
                                 fill_mid=None, marks={}))
            for o in list(live):
                if o["state"] == "queued" and now - o["born"] > MAXWAIT:
                    o["state"] = "expired"
                    done.append(o); live.remove(o)
                elif o["state"] == "filled":
                    for h in MARKOUTS:
                        if h not in o["marks"] and now - o["fill_ts"] >= h:
                            m = mid(o["tok"])
                            o["marks"][h] = None if m is None else m - o["price"]
                    if len(o["marks"]) == len(MARKOUTS):
                        done.append(o); live.remove(o)
            await asyncio.sleep(1)

    async def feed():
        while time.time() < stop:
            toks = list(meta)
            if not toks:
                await asyncio.sleep(3); continue
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
                                t = m["asset_id"]
                                books[t] = {float(x["price"]): float(x["size"])
                                            for x in m.get("bids", []) if float(x["size"]) > 0}
                                asks[t] = {float(x["price"]): float(x["size"])
                                           for x in m.get("asks", []) if float(x["size"]) > 0}
                                if books[t]:
                                    p = max(books[t]); best[t] = (p, books[t][p])
                            elif et == "price_change":
                                for c in m.get("price_changes", []):
                                    t = c.get("asset_id")
                                    if not t:
                                        continue
                                    bk = (books if c.get("side") == "BUY" else asks).setdefault(t, {})
                                    px, sz = float(c["price"]), float(c["size"])
                                    bk.pop(px, None) if sz <= 0 else bk.update({px: sz})
                                    if c.get("side") == "BUY" and books.get(t):
                                        p = max(books[t]); best[t] = (p, books[t][p])
                            elif et == "last_trade_price" and m.get("side") == "SELL":
                                t, px = m.get("asset_id"), float(m["price"])
                                for o in live:
                                    if (o["state"] == "queued" and o["tok"] == t
                                            and px <= o["price"] + 1e-9):
                                        o["sold"] += float(m["size"])
                                        if o["sold"] > 0 and o["sold"] >= o["ahead"]:
                                            o["state"] = "filled"
                                            o["fill_ts"] = time.time()
                                            o["fill_mid"] = mid(t)
            except Exception:
                await asyncio.sleep(3)

    await asyncio.gather(refresh(), poster(), feed())
    json.dump(done, open("makercheck.json", "w"))
    fills = [o for o in done if o["state"] == "filled"]
    print(f"\n=== {len(done)} posts, {len(fills)} filled "
          f"({len(fills)/max(len(done),1)*100:.1f}%) ===")
    if not done:
        return
    print(f"  {'5-MINUTE' if FIVE else 'hourly'} markets, improve {IMPROVE} "
          f"tick(s), min capture {MINCAP:+.3f}")
    if FIVE and fills:
        reb = statistics.mean(0.07 * o["price"] * (1 - o["price"]) for o in fills)
        print(f"  maker fee-share rebate (100% of the taker fee at fill price):"
              f" +{reb*100:.2f}c/share")
    print(f"  size ahead at post  median {statistics.median([o['ahead'] for o in done]):.0f} sh")
    cap = [o.get("capture") for o in done if o.get("capture") is not None]
    if cap:
        print(f"  capture at post     median {statistics.median(cap)*100:+.2f}c"
              f"   mean {statistics.mean(cap)*100:+.2f}c")
    for h in MARKOUTS:
        v = [o["marks"][h] for o in fills if o["marks"].get(h) is not None]
        if len(v) < 5:
            print(f"  markout {h:>5.0f}s   n={len(v)} -- too few")
            continue
        mu = statistics.mean(v)
        sd = statistics.stdev(v) if len(v) > 1 else 0.0
        t = mu / (sd / len(v) ** 0.5) if sd else 0.0
        print(f"  markout {h:>5.0f}s   n={len(v):>4}  mean {mu*100:+.3f}c"
              f"  median {statistics.median(v)*100:+.3f}c  t={t:+.2f}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 60))
