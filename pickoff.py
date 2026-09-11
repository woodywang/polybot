"""Are there REST-confirmed mispriced quotes, and are they worth crossing?

Section 54: the account this project started from is a taker clearing 1.64c a
share where the displayed quote returns -1.7c, and frozen quotes are the natural
explanation. But `stalecheck.py` showed the websocket book disagrees with the
CLOB REST book a quarter of the time, by up to 14 cents, and always in the
flattering direction -- so a frozen websocket quote is not evidence of anything.

This confirms before believing. When the websocket says a book has not moved and
the model says its ask is far from fair, poll REST for that token. Only quotes
both views agree on are counted. Hourly markets only: the strike is the Binance
1h candle open, read exactly, so the model is a plain spot digital with no TWAP
approximation and no reconstructed strike.

Writes pickoff.json: every confirmed and rejected candidate, with the settlement
looked up afterwards by `report()`.
"""
import asyncio, json, math, time, urllib.request, sys
from collections import deque
import websockets
import fair, run

UA = run.UA
CLOB = "https://clob.polymarket.com"
MINAGE = 5.0            # seconds a book must have sat still to be a candidate
MINGAP = 0.03           # model-vs-ask gap worth a REST call, in dollars


def get(u, timeout=10):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA),
                                    timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def rest_book(tok):
    b = get(f"{CLOB}/book?token_id={tok}")
    if not b:
        return None, None
    def best(side, pick):
        v = [float(x["price"]) for x in (b.get(side) or [])
             if float(x.get("size", 0)) > 0]
        return pick(v) if v else None
    return best("bids", max), best("asks", min)


async def main(minutes=45):
    st_books, meta, spot, sig = {}, {}, {}, {}
    out, seen = [], {}
    stop = time.time() + minutes * 60

    async def binance():
        url = ("wss://stream.binance.com:9443/stream?streams="
               + "/".join(f"{s}@trade" for s in ("btcusdt", "ethusdt", "solusdt")))
        ticks = {}
        while time.time() < stop:
            try:
                async with websockets.connect(url, ping_interval=None) as ws:
                    while time.time() < stop:
                        d = json.loads(await asyncio.wait_for(ws.recv(), 20))["data"]
                        s, p = d["s"].lower(), float(d["p"])
                        spot[s] = p
                        q = ticks.setdefault(s, deque(maxlen=40_000))
                        q.append((time.time(), p))
                        # fair.realized_sigma buckets at 5s on purpose: sampling
                        # at 1s measures bid-ask bounce, not volatility, and read
                        # a true sigma of 0.50 as 0.81.
                        if len(q) > 100:
                            sig[s] = fair.realized_sigma(q)
            except Exception:
                await asyncio.sleep(2)

    async def markets():
        while time.time() < stop:
            base = int(time.time() // 3600) * 3600
            for a in ("btc", "eth", "sol"):
                for k in (0, 1):
                    slug = run.hourly_slug(a, base + k * 3600)
                    if slug in meta:
                        continue
                    m = await asyncio.to_thread(
                        get, f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=false")
                    if not m:
                        continue
                    try:
                        op = await asyncio.to_thread(
                            run.kline_open, run.BN[a], base + k * 3600)
                    except Exception:
                        op = None
                    if not op:
                        continue
                    t = json.loads(m[0]["clobTokenIds"])
                    meta[slug] = dict(asset=a, start=base + k * 3600, strike=op,
                                      up=t[0], dn=t[1])
            await asyncio.sleep(120)

    async def feed():
        while time.time() < stop:
            toks = [t for m in meta.values() for t in (m["up"], m["dn"])]
            if not toks:
                await asyncio.sleep(3); continue
            try:
                async with websockets.connect(
                        "wss://ws-subscriptions-clob.polymarket.com/ws/market",
                        ping_interval=None) as ws:
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
                                st_books.setdefault(m["asset_id"], run.Book()).snapshot(m)
                            elif et == "price_change":
                                for c in m.get("price_changes", []):
                                    if c.get("asset_id"):
                                        st_books.setdefault(
                                            c["asset_id"], run.Book()).level(c)
            except Exception:
                await asyncio.sleep(2)

    async def scan():
        while time.time() < stop:
            now = time.time()
            for slug, mk in list(meta.items()):
                tau = mk["start"] + 3600 - now
                s = spot.get(run.BN[mk["asset"]].lower())
                sg = sig.get(run.BN[mk["asset"]].lower())
                if not s or not sg or not (60 < tau < 3600):
                    continue
                p_up = fair.fair_up_naive(s, mk["strike"], tau, sg)
                for side, tok, p in (("Up", mk["up"], p_up),
                                     ("Down", mk["dn"], 1 - p_up)):
                    bk = st_books.get(tok)
                    if not bk:
                        continue
                    ask, dep = bk.best_ask()
                    age = bk.stale_for(now)
                    if ask is None or dep <= 0 or age < MINAGE:
                        continue
                    gap = p - ask - fair.taker_fee(ask)
                    if gap < MINGAP:
                        continue
                    if now - seen.get(tok, 0) < 20:
                        continue
                    seen[tok] = now
                    rb, ra = await asyncio.to_thread(rest_book, tok)
                    out.append(dict(ts=now, slug=slug, side=side, tau=tau,
                                    ws_ask=ask, rest_ask=ra, depth=dep,
                                    age=age, fair=p, gap=gap, spot=s, sigma=sg,
                                    strike=mk["strike"],
                                    ok=(ra is not None and abs(ra - ask) < 1e-9)))
                    json.dump(out, open("pickoff.json", "w"))
            await asyncio.sleep(1)

    await asyncio.gather(binance(), markets(), feed(), scan())
    print(f"{len(out)} candidates")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 45))
