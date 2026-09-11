"""Paper-trade Polymarket 5-minute crypto Up/Down.

Two strategies run side by side on the same feed:

  open  -- take a side when the TWAP-digital fair value beats the ask by more
           than the taker fee.  This is the directional leg.
  lock  -- once a side is held, buy the OPPOSITE side later in the window if
           the two legs together cost less than $1.  That converts an open
           position into a guaranteed dollar.

The lock is not arbitrage.  Up and Down are complementary, so at any single
instant ask_up + ask_down >= 1 + spread and buying both costs more than it
pays.  A pair only comes in under a dollar if the price moved between the two
legs, which means the lock is just a way of realising a directional gain
without crossing the spread to sell.  The edge still has to come from the
model picking the first leg.  The `pairs` table records the simultaneous
two-sided cost every tick so the run can say how often true arbitrage existed
(expected answer: never).

Settlement is Chainlink's btc-usd-twap-60s stream, which needs credentials we
do not have.  Binance and Coinbase are consolidated by median as a stand-in
for Chainlink's cross-venue aggregation, and the on-chain Chainlink feed is
polled purely to measure the basis we are carrying.

No orders are sent.
"""
import argparse, asyncio, json, sqlite3, statistics, sys, time, urllib.request
from collections import deque

import websockets
import fair

GAMMA = "https://gamma-api.polymarket.com/markets"
BINANCE = "wss://stream.binance.com:9443/stream?streams="
COINBASE = "wss://ws-feed.exchange.coinbase.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RPC = "https://polygon.drpc.org"
CL_FEED = {"btc": "0xc907E116054Ad103354f2D350FD2514433D57F6f"}   # BTC/USD, 8dp
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
WIN = 300                        # 5-minute markets
HIST = 420                       # seconds of tape to keep
TWAP_W = 60.0                    # chainlink twap-60s stream
HOURLY_NAME = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana",
               "xrp": "xrp", "doge": "dogecoin", "bnb": "bnb"}
ET_OFFSET = -4 * 3600          # EDT; the slug names the hour in Eastern time
BN = {"btc": "btcusdt", "eth": "ethusdt", "sol": "solusdt",
      "xrp": "xrpusdt", "doge": "dogeusdt", "bnb": "bnbusdt"}
CB = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD",
      "xrp": "XRP-USD", "doge": "DOGE-USD"}

DDL = """
CREATE TABLE IF NOT EXISTS markets(
  slug TEXT PRIMARY KEY, asset TEXT, start_ts INT, tok_up TEXT, tok_dn TEXT,
  strike REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS regime(
  slug TEXT PRIMARY KEY, er1 REAL, er5 REAL, rng1 REAL, path1 REAL);
CREATE TABLE IF NOT EXISTS obs(
  ts REAL, slug TEXT, kind TEXT, tau REAL, spot REAL, strike REAL, sigma REAL,
  fair_up REAL, naive_up REAL, side TEXT, ask REAL, depth REAL, edge REAL,
  fill_px REAL, fill_sz REAL, fee REAL, intens REAL, tps REAL,
  isig REAL, band REAL, equity REAL, stale REAL);
CREATE INDEX IF NOT EXISTS obs_slug ON obs(slug);
CREATE TABLE IF NOT EXISTS pairs(
  ts REAL, slug TEXT, tau REAL, ask_up REAL, ask_dn REAL, cost REAL,
  intens REAL, tps REAL);
CREATE TABLE IF NOT EXISTS matched(
  ts REAL, slug TEXT, shares REAL, cost_open REAL, cost_lock REAL, pair REAL);
CREATE TABLE IF NOT EXISTS equity(
  ts REAL, slug TEXT, pnl REAL, equity REAL, committed REAL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS basis(
  ts REAL, asset TEXT, venue REAL, chainlink REAL, bps REAL);
"""


def gamma(slugs, closed=None):
    if not slugs:
        return []
    q = "&".join(f"slug={s}" for s in slugs)
    if closed is not None:
        q += f"&closed={'true' if closed else 'false'}"
    try:
        req = urllib.request.Request(f"{GAMMA}?{q}", headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception:
        return []


class Book:
    """Snapshot from `book`, then per-level deltas from `price_change`.

    The delta envelope has no asset_id -- it lives on each entry, and the field
    is `price_changes`, not `changes`.  Get either wrong and the book silently
    freezes on its opening snapshot while every quote read goes stale.
    """
    __slots__ = ("bids", "asks", "hint", "touched")

    def __init__(self):
        self.bids, self.asks, self.hint = {}, {}, None
        self.touched = 0.0            # last time this book actually changed

    def snapshot(self, msg):
        self.bids = {float(x["price"]): float(x["size"]) for x in msg.get("bids", [])}
        self.asks = {float(x["price"]): float(x["size"]) for x in msg.get("asks", [])}
        self.touched = time.time()

    def level(self, c):
        side = self.bids if c.get("side") == "BUY" else self.asks
        px, sz = float(c["price"]), float(c["size"])
        if sz <= 0:
            side.pop(px, None)
        else:
            side[px] = sz
        if c.get("best_ask") not in (None, ""):
            self.hint = float(c["best_ask"])
        self.touched = time.time()

    def stale_for(self, now=None):
        """Seconds since this book last moved.

        Quotes stop updating near expiry -- change rate falls from 29% per
        snapshot at 60-120s to 4.9% under 30s, and several markets showed zero
        changes across 180+ consecutive snapshots in their last minute. Those
        frozen prices are not tradeable, but they look like enormous edge: a
        book left at 0.51/0.50 while spot is 30bps away reads as a 45-cent
        mispricing and produced a 100% win rate that was pure artifact.
        """
        return (now or time.time()) - self.touched if self.touched else 1e9

    def best_bid(self):
        live = [p for p, s in self.bids.items() if s > 0]
        if not live:
            return None, 0.0
        p = max(live)
        return p, self.bids[p]

    def best_ask(self):
        live = [p for p, s in self.asks.items() if s > 0]
        if self.hint is not None:
            live = [p for p in live if p >= self.hint]
        if not live:
            return (self.hint, 0.0) if self.hint else (None, 0.0)
        p = min(live)
        return p, self.asks[p]


class State:
    def __init__(self, a):
        self.cfg = a
        self.ticks = {k: deque(maxlen=400_000) for k in a.assets}
        self.flow = {k: deque(maxlen=400_000) for k in a.assets}   # (ts, notional)
        self.venue = {k: {} for k in a.assets}      # asset -> venue -> last px
        self.markets, self.books = {}, {}
        self.db = sqlite3.connect(a.db)
        self.db.executescript(DDL)
        # `obs` has gained columns three times; older files predate each one.
        if "stale" not in {r[1] for r in self.db.execute("PRAGMA table_info(obs)")}:
            self.db.execute("ALTER TABLE obs ADD COLUMN stale REAL")
        # A report has to know the bankroll the arm actually ran with, or a
        # drawdown computed against argparse's default is wrong by whatever
        # factor separates them -- it read 2.9% instead of 29% here.  Store the
        # configuration with the data it produced.
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('bankroll',?)",
                        (str(a.bankroll),))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('argv',?)",
                        (" ".join(sys.argv[1:]),))
        self.db.commit()
        self.wanted, self.gen = set(), 0
        self.pos = {}                 # (slug, side) -> [shares, cost_incl_fee]
        self.lots = {}                # (slug, side) -> deque([shares, cost/share])
        self.pending = set()          # (slug, side) with a fill in flight
        self.rest = {}                # token -> resting simulated bid
        self.kl = {k: {} for k in a.assets}        # asset -> interval -> closes
        # Real money management: an account of `bankroll` dollars cannot deploy
        # more than it holds, and capital stays committed until the market it
        # is in settles. Peak simultaneous exposure -- not cumulative turnover
        # -- is the number that has to be funded.
        self.equity = a.bankroll
        self.committed = 0.0
        self.mkt_cost = {}                         # slug -> capital tied up
        self.peak = a.bankroll
        self.halted = False
        self._schema_check()       # last: it exercises every field above

    def _schema_check(self):
        """Write one row of every shape at boot and roll it back.

        Every table here has gained columns mid-run at least once, and a
        positional INSERT that drifts only fails when the first live row is
        written -- minutes in, after the process looked healthy."""
        snap = dict(tau=0, spot=0, strike=0, sigma=0, fair_up=.5, naive_up=.5,
                    intens=1, tps=0, isig=None, band=None)
        try:
            log(self, "sample", "__schema__", snap, "Up", 0.5, 1.0, 0.0)
            log(self, "open", "__schema__", snap, "Up", 0.5, 1.0, 0.0, 1.0)
            self.db.execute("INSERT INTO pairs VALUES(?,?,?,?,?,?,?,?)",
                            (0, "__schema__", 0, .5, .5, 1., 1., 0.))
            self.db.execute("INSERT INTO matched VALUES(?,?,?,?,?,?)",
                            (0, "__schema__", 0., 0., 0., 0.))
            self.db.execute("INSERT INTO equity VALUES(?,?,?,?,?)",
                            (0, "__schema__", 0., 0., 0.))
            self.db.execute("INSERT INTO regime VALUES(?,?,?,?,?)",
                            ("__schema__", 0., 0., 0., 0.))
            self.db.execute("INSERT INTO basis VALUES(?,?,?,?,?)",
                            (0, "x", 0., 0., 0.))
        finally:
            self.db.rollback()
            for t in ("obs", "pairs", "matched", "equity", "regime"):
                self.db.execute(f"DELETE FROM {t} WHERE slug='__schema__'")
            self.db.commit()
        self.pos.clear(); self.lots.clear()
        self.committed = 0.0; self.mkt_cost.clear()

    def push(self, asset, venue, ts, px, qty=0.0):
        """Consolidate venues by median -- Chainlink aggregates across spot
        venues, so a single exchange carries a basis the model cannot see."""
        self.venue[asset][venue] = px
        q = self.ticks[asset]
        q.append((ts, statistics.median(self.venue[asset].values())))
        cut = ts - HIST
        while q and q[0][0] < cut:
            q.popleft()
        f = self.flow[asset]
        f.append((ts, px * qty))
        while f and f[0][0] < cut:
            f.popleft()

    def intensity(self, asset, recent=60.0, base=420.0):
        """Trade arrival rate over the last `recent` seconds relative to its
        own baseline.  Under the mixture-of-distributions view variance per
        unit time scales with the trade arrival rate, so sqrt of this is the
        natural multiplier on a trailing sigma to make it forward looking.
        Returns (rate_ratio, notional_ratio, trades_per_sec)."""
        f = self.flow[asset]
        if not f:
            return 1.0, 1.0, 0.0
        now = f[-1][0]
        rn = [x for x in f if x[0] >= now - recent]
        bn = [x for x in f if x[0] >= now - base]
        if len(rn) < 20 or len(bn) < 60:
            return 1.0, 1.0, len(rn) / recent
        span = max(bn[-1][0] - bn[0][0], 1.0)
        r_cnt = (len(rn) / recent) / max(len(bn) / span, 1e-9)
        r_not = (sum(x[1] for x in rn) / recent) / \
                max(sum(x[1] for x in bn) / span, 1e-9)
        return r_cnt, r_not, len(rn) / recent

    def spot(self, asset):
        t = self.ticks[asset]
        return t[-1][1] if t else None

    def px_at(self, asset, when):
        prev = None
        for ts, px in self.ticks[asset]:
            if ts > when:
                break
            prev = px
        return prev

    def integral(self, asset, t0, t1):
        """Trapezoid of price over [t0, t1]; the printed part of the TWAP."""
        pts = [(ts, px) for ts, px in self.ticks[asset] if t0 <= ts <= t1]
        head = self.px_at(asset, t0)
        if head is not None:
            pts.insert(0, (t0, head))
        if not pts:
            return None
        if pts[-1][0] < t1:
            pts.append((t1, pts[-1][1]))
        return sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(pts, pts[1:]))

    def held(self, slug, side):
        return self.pos.get((slug, side), [0.0, 0.0])

    def lots_of(self, slug, side):
        return self.lots.setdefault((slug, side), deque())

    def unmatched(self, slug, side):
        return sum(l[0] for l in self.lots_of(slug, side))

    def fifo_basis(self, slug, side):
        """Cost of the next share we would hedge.

        Deliberately NOT the running average.  The open strategy keeps adding
        to a position after part of it has been hedged, and averaging lets
        those later, worse fills leak backwards into pairs that were already
        locked -- which is how a run with min_lock=1.5c still ended up paying
        $1.0084 per dollar.  Lots are consumed FIFO and retired on match."""
        q = self.lots_of(slug, side)
        return q[0][1] if q else None

    def take_lots_peek(self, slug, side, n):
        """What take_lots would return, without consuming anything."""
        got, cost = 0.0, 0.0
        for sh, cps in self.lots_of(slug, side):
            if got >= n - 1e-12:
                break
            use = min(sh, n - got)
            got += use
            cost += use * cps
        return got, cost

    def take_lots(self, slug, side, n):
        """Peel n shares FIFO, returning their total cost."""
        q, got, cost = self.lots_of(slug, side), 0.0, 0.0
        while q and got < n - 1e-12:
            sh, cps = q[0]
            use = min(sh, n - got)
            got += use
            cost += use * cps
            if use >= sh - 1e-12:
                q.popleft()
            else:
                q[0][0] -= use
        return got, cost


# --------------------------------------------------------------- feeds
async def feed_binance(st):
    url = BINANCE + "/".join(f"{BN[a]}@trade" for a in st.cfg.assets)
    rev = {BN[a]: a for a in st.cfg.assets}
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                print("[binance] connected", flush=True)
                async for raw in ws:
                    m = json.loads(raw)
                    d = m.get("data", m)
                    a = rev.get(m.get("stream", "").split("@")[0])
                    if a and "p" in d:
                        st.push(a, "binance", d["T"] / 1000.0, float(d["p"]),
                                float(d.get("q", 0)))
        except Exception as e:
            print(f"[binance] {type(e).__name__}: {e}; retry", flush=True)
            await asyncio.sleep(2)


async def feed_coinbase(st):
    prods = [CB[a] for a in st.cfg.assets if a in CB]
    rev = {CB[a]: a for a in st.cfg.assets if a in CB}
    while True:
        try:
            async with websockets.connect(COINBASE, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "subscribe",
                                          "product_ids": prods,
                                          "channels": ["matches"]}))
                print(f"[coinbase] subscribed {prods}", flush=True)
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") == "match" and m.get("price"):
                        a = rev.get(m.get("product_id"))
                        if a:
                            st.push(a, "coinbase", time.time(), float(m["price"]),
                                    float(m.get("size", 0)))
        except Exception as e:
            print(f"[coinbase] {type(e).__name__}: {e}; retry", flush=True)
            await asyncio.sleep(3)


def regime_of(closes):
    """Two orthogonal numbers from one candle series.

      er    = |net move| / path walked -- DIRECTION.  1 = clean trend, 0 = chop.
      path  = total distance walked, in bps -- AMPLITUDE.

    One alone is not enough: a market can drift 5bps with er=1 (nothing to
    trade) or swing 200bps with er=0 (everything to trade).  The two strategies
    want opposite corners of this grid -- the directional leg wants high er,
    the sequential lock wants low er with a long path, because it needs the
    implied probability to visit both extremes inside one window.
    """
    if len(closes) < 3:
        return None, None
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    if path <= 0 or closes[-1] <= 0:
        return 0.0, 0.0
    return abs(closes[-1] - closes[0]) / path, path / closes[-1] * 1e4


def hourly_slug(asset, start_utc):
    """bitcoin-up-or-down-september-13-2026-3pm-et for the candle starting then."""
    t = time.gmtime(start_utc + ET_OFFSET)
    ampm = "am" if t.tm_hour < 12 else "pm"
    h12 = t.tm_hour % 12 or 12
    month = ("january february march april may june july august september "
             "october november december").split()[t.tm_mon - 1]
    return (f"{HOURLY_NAME[asset]}-up-or-down-{month}-{t.tm_mday}-"
            f"{t.tm_year}-{h12}{ampm}-et")


def kline_open(sym, start_utc):
    """The exact open of the 1h candle beginning at start_utc.

    Hourly markets settle on the Binance 1h candle -- close versus open -- so
    unlike the 5-minute markets the strike is not estimated at all. The three
    largest error sources there (a 5.3bps venue basis, the TWAP approximation,
    and a strike reconstructed from a 60s integral) are all exactly zero here.
    """
    u = (f"https://api.binance.com/api/v3/klines?symbol={sym.upper()}"
         f"&interval=1h&startTime={int(start_utc)*1000}&limit=1")
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        k = json.load(r)
    if not k or int(k[0][0]) // 1000 != int(start_utc):
        return None
    return float(k[0][1])


def _klines(sym, interval, limit):
    u = (f"https://api.binance.com/api/v3/klines?symbol={sym.upper()}"
         f"&interval={interval}&limit={limit}")
    req = urllib.request.Request(u, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return [float(k[4]) for k in json.load(r)]        # closes


async def poll_klines(st):
    """Regime context the tick tape cannot give: we only keep 7 minutes of
    ticks, and a trend/chop read needs an hour."""
    while True:
        for a in st.cfg.assets:
            for iv, n in (("1m", 60), ("5m", 36)):
                try:
                    st.kl[a][iv] = await asyncio.to_thread(_klines, BN[a], iv, n)
                except Exception:
                    pass
        await asyncio.sleep(30)


def _chainlink(addr):
    """latestRoundData() -> (roundId, answer, startedAt, updatedAt, ...)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": addr, "data": "0xfeaf968c"}, "latest"]})
    req = urllib.request.Request(RPC, data=body.encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=15) as r:
        res = json.load(r).get("result", "")
    if len(res) < 2 + 64 * 2:
        return None
    raw = int(res[2 + 64:2 + 128], 16)
    if raw >= 1 << 255:
        raw -= 1 << 256
    return raw / 1e8                       # feed has 8 decimals


async def poll_chainlink(st):
    """Diagnostic only.  This is the 27s-heartbeat price feed, not the
    twap-60s stream Polymarket settles on, so it is far too slow to trade.
    It is here to measure the basis our consolidated spot is carrying."""
    while True:
        for a, addr in CL_FEED.items():
            if a not in st.cfg.assets:
                continue
            try:
                cl = await asyncio.to_thread(_chainlink, addr)
                sp = st.spot(a)
                if cl and sp:
                    st.db.execute("INSERT INTO basis VALUES(?,?,?,?,?)",
                                  (time.time(), a, sp, cl, (sp - cl) / cl * 1e4))
                    st.db.commit()
            except Exception:
                pass
        await asyncio.sleep(15)


async def feed_books(st):
    while True:
        toks, gen = sorted(st.wanted), st.gen
        if not toks:
            await asyncio.sleep(1)
            continue
        try:
            # max_queue=None: the 5-minute books push 867 frames/s and 570 KB/s
            # across 18 tokens -- 19x the hourly markets -- and the library's
            # default 32-frame queue applies backpressure long before the
            # consumer is done, so the server's send buffer fills and it closes
            # us with 1013 "slow consumer".  That happened 309 times across the
            # fleet and is the most likely source of the websocket book
            # disagreeing with the REST book by 14c in section 54.
            async with websockets.connect(POLY_WS, ping_interval=None,
                                          max_queue=None) as ws:
                await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                print(f"[poly] subscribed {len(toks)} tokens", flush=True)

                async def ping():
                    while True:
                        await asyncio.sleep(10)
                        await ws.send("PING")

                pt = asyncio.create_task(ping())
                try:
                    while st.gen == gen:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        if raw == "PONG":
                            continue
                        msgs = json.loads(raw)
                        for m in msgs if isinstance(msgs, list) else [msgs]:
                            et = m.get("event_type")
                            if et == "book" and m.get("asset_id"):
                                st.books.setdefault(m["asset_id"], Book()).snapshot(m)
                            elif et == "price_change":
                                for c in m.get("price_changes", []):
                                    if c.get("asset_id"):
                                        st.books.setdefault(
                                            c["asset_id"], Book()).level(c)
                            elif et == "last_trade_price" and st.cfg.maker:
                                # A sell at or below a resting bid is flow that
                                # has to clear the queue ahead of it before it
                                # reaches us.  This is the only fill evidence
                                # the public feed gives.
                                if m.get("side") != "SELL":
                                    continue
                                o = st.rest.get(m.get("asset_id"))
                                if o and float(m["price"]) <= o["price"] + 1e-9:
                                    o["sold"] += float(m["size"])
                finally:
                    pt.cancel()
        except Exception as e:
            print(f"[poly] {type(e).__name__}: {e}; retry", flush=True)
            await asyncio.sleep(2)


# ---------------------------------------------------------- market plumbing
async def discover(st):
    """Slugs are deterministic: {asset}-updown-5m-{window_start}."""
    while True:
        now = time.time()
        win = st.cfg.window
        base = int(now // win) * win
        if st.cfg.hourly:
            slugs = [hourly_slug(a, base + k * win)
                     for a in st.cfg.assets for k in (0, 1)]
        else:
            slugs = [f"{a}-updown-5m-{base + k * win}"
                     for a in st.cfg.assets for k in (0, 1, 2)]
        new = [s for s in slugs if s not in st.markets]
        for m in await asyncio.to_thread(gamma, new, False):
            slug = m["slug"]
            toks = json.loads(m["clobTokenIds"])
            if st.cfg.hourly:
                # endDate is the candle's close; the candle opened an hour before
                end = time.strptime(m["endDate"][:19], "%Y-%m-%dT%H:%M:%S")
                start = int(time.mktime(end) - time.timezone) - st.cfg.window
                asset = next((a for a in st.cfg.assets
                              if slug.startswith(HOURLY_NAME[a] + "-")), None)
                if asset is None:
                    continue
            else:
                asset, start = slug.split("-")[0], int(slug.split("-")[-1])
            st.markets[slug] = dict(asset=asset, start=start,
                                    up=toks[0], dn=toks[1], strike=None)
        live = {t for s, mk in st.markets.items() for t in (mk["up"], mk["dn"])
                if mk["start"] + st.cfg.window > now - 30}
        if live != st.wanted:
            st.wanted, st.gen = live, st.gen + 1
        for slug in [s for s, mk in st.markets.items()
                     if mk["start"] + st.cfg.window < now - 600]:
            del st.markets[slug]
        await asyncio.sleep(15)


async def resolve(st):
    while True:
        rows = st.db.execute(
            "SELECT slug, start_ts FROM markets WHERE outcome IS NULL").fetchall()
        due = [s for s, t in rows if time.time() > t + st.cfg.window + 120]
        got = 0
        # One slug per request: gamma silently returns [] when several slug
        # params are combined with closed=true, which reads as "nothing has
        # settled yet" forever.  Polymarket also flips `closed` about 4-6
        # minutes after the window ends, so passes before that find nothing.
        for slug in due:
            # Two settlement clocks.  The 5-minute markets flip `closed` within
            # minutes of expiry and print a clean {"1","0"}.  The hourly ones go
            # through UMA: the proposal lands right after expiry with
            # outcomePrices already at 0.9995/0.0005, but `closed` stays false
            # for the two-hour dispute window -- so waiting on `closed` never
            # settles an hourly market at all.  Reading the price instead is
            # safe here only because `due` has already established the window
            # ended over two minutes ago; before expiry an extreme quote is a
            # live price, after it, it is the resolution.
            ms = (await asyncio.to_thread(gamma, [slug], True)
                  or await asyncio.to_thread(gamma, [slug], False))
            for m in ms:
                try:
                    pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                except (TypeError, ValueError):
                    continue
                if len(pr) == 2 and max(pr) >= 0.99:
                    won = "Up" if pr[0] > pr[1] else "Down"
                    st.db.execute("UPDATE markets SET outcome=? WHERE slug=?",
                                  (won, m["slug"]))
                    got += 1
                    sl = m["slug"]
                    cost = st.mkt_cost.pop(sl, 0.0)
                    if cost:
                        payout = st.held(sl, won)[0]        # $1 per winning share
                        st.committed = max(st.committed - cost, 0.0)
                        st.equity += payout - cost
                        st.peak = max(st.peak, st.equity)
                        # Circuit breaker. Bootstrapping the 87 settled markets
                        # showed the realised drawdown sitting at the 1.4th
                        # percentile of random reorderings of the same returns:
                        # losses cluster, so an iid envelope is too loose and a
                        # hard equity floor is the honest guard. Sizing cannot
                        # lean on the edge either -- mean 4.26% per market with
                        # a 2.79% standard error is t = 1.53, so its 95% lower
                        # bound is negative and any Kelly fraction computed off
                        # it is betting estimation error.
                        dd = (st.peak - st.equity) / max(st.peak, 1e-9)
                        if not st.halted and dd >= st.cfg.max_dd > 0:
                            st.halted = True
                            print(f"[HALT] drawdown {dd*100:.1f}% >= "
                                  f"{st.cfg.max_dd*100:.0f}%; opening stopped, "
                                  f"hedging continues", flush=True)
                        st.db.execute("INSERT INTO equity VALUES(?,?,?,?,?)",
                                      (time.time(), sl, payout - cost,
                                       st.equity, st.committed))
                    for k in [k for k in st.pos if k[0] == sl]:
                        st.pos.pop(k, None); st.lots.pop(k, None)
        st.db.commit()
        if got:
            print(f"[resolve] open={len(rows)} due={len(due)} settled={got}",
                  flush=True)
        await asyncio.sleep(60)


# --------------------------------------------------------------- trading
def log(st, kind, slug, snap, side, ask, depth, ed, sz=None, fee=None,
        stale=None):
    # A maker pays no taker fee, so the fee is passed in rather than derived.
    # `0.0` and `None` mean different things here and `or` would conflate them.
    if fee is None:
        fee = fair.taker_fee(ask, sz) if sz else None
    # Named columns, not positional: the table has grown three times and a
    # positional INSERT drifts silently until it hits a live row.
    st.db.execute(
        "INSERT INTO obs(ts,slug,kind,tau,spot,strike,sigma,fair_up,naive_up,"
        "side,ask,depth,edge,fill_px,fill_sz,fee,intens,tps,isig,band,equity,"
        "stale) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), slug, kind, snap["tau"], snap["spot"],
                   snap["strike"], snap["sigma"], snap["fair_up"],
                   snap["naive_up"], side, ask, depth, ed,
                   ask if sz else None, sz, fee,
                   snap.get("intens"), snap.get("tps"),
                   snap.get("isig"), snap.get("band"), st.equity, stale))
    if sz:
        st.committed += ask * sz + fee
        st.mkt_cost[slug] = st.mkt_cost.get(slug, 0.0) + ask * sz + fee
        p = st.pos.setdefault((slug, side), [0.0, 0.0])
        p[0] += sz
        p[1] += ask * sz + fee
        cps = ask + fee / sz
        if kind == "open":
            st.lots_of(slug, side).append([sz, cps])
        else:                                    # lock: retire the pair
            other = "Down" if side == "Up" else "Up"
            got, c_open = st.take_lots(slug, other, sz)
            if got > 0:
                st.db.execute("INSERT INTO matched VALUES(?,?,?,?,?,?)",
                              (time.time(), slug, got, c_open, cps * got,
                               c_open + cps * got))


async def fill(st, kind, slug, side, tok, snap, cap):
    """Wait out the round trip, then fill at whatever the book actually is.
    This is the whole difference between a backtest and a fantasy."""
    try:
        await asyncio.sleep(st.cfg.latency_ms / 1000.0)
        ask, depth = st.books.get(tok, Book()).best_ask()
        if ask is None or depth <= 0:
            return
        # Measured adverse selection, charged rather than assumed away. Over
        # 5,356 samples the quotes the model wanted moved +0.130c against us in
        # 250ms while the ones it passed on moved 0.343c in our favour -- the
        # price that looks mispriced is not the price you get. It is about 8%
        # of a 1.6c gross edge, so the paper fill pays it.
        ask = min(ask + st.cfg.slippage, 0.999)
        if kind == "open":
            ed = fair.edge(snap["fair"], ask)
            # Re-check at fill time, since the price can move between the
            # signal and the order.  `min(0, min_edge)` keeps the usual bar at
            # zero and opens the gate only for a deliberately negative
            # --min-edge, which is how a rule that ignores the model entirely
            # (buy any side in a price band) gets tested without the model
            # quietly vetoing half its entries.
            if ed <= min(0.0, st.cfg.min_edge) or ask > st.cfg.max_price:
                return
            room = st.cfg.max_per_market - st.held(slug, side)[1]
            room = min(room, free_capital(st))    # cannot spend what is committed
            stake = min(st.cfg.max_usd,
                        kelly_usd(snap["fair"], ask, st.cfg.bankroll,
                                  st.cfg.kelly) if st.cfg.kelly > 0
                        else st.cfg.max_usd)
            sz = min(stake / ask, depth, max(room, 0.0) / ask)
        else:                                    # lock
            ed = 1.0 - cap - ask - fair.taker_fee(ask)
            if ed < st.cfg.min_lock:
                return                           # the move went away
            sz = min(st.cfg.max_usd / ask, depth, cap_shares(st, slug, side),
                     free_capital(st) / ask)
            if sz > 0:          # re-price against the exact lots we will consume
                _, c = st.take_lots_peek(slug,
                                         "Down" if side == "Up" else "Up", sz)
                if 1.0 - (c / sz) - ask - fair.taker_fee(ask) < (snap.get("band")
                    or st.cfg.min_lock):
                    return
        if sz > 0:
            log(st, kind, slug, snap, side, ask, depth, ed, sz)
            st.db.commit()
    finally:
        st.pending.discard((slug, side))


def maker_step(st, slug, side, tok, snap, now, mid):
    """Simulate one resting bid at the touch, and fill it when the queue clears.

    Section 49: the hourly book's mid under-prices the favourite by ~4.3 points
    over 0.55-0.65, and the taker fee plus half-spread is larger than that. A
    maker pays no fee and is handed the half-spread instead of paying it, which
    turns the same edge from +1.6c (t=1.5) into +5.3c (t=4.8) -- *if* fills
    arrive independently of what happens next, which is the part no historical
    file can answer.

    Queue model: a joiner sits behind the size already resting at that price and
    fills once cumulative sells at or below it exceed that size.  Moving the
    best bid cancels and replaces, which resets queue position -- what actually
    happens to a real order.  `last_trade_price` is the only fill evidence the
    public feed gives and it may report only price *changes*, so detected volume
    is a lower bound and so is the fill rate.
    """
    bk = st.books.get(tok)
    bid, qsz = bk.best_bid() if bk else (None, 0.0)
    # Section 57: behind a 100-share queue you fill only when someone sweeps,
    # and markout was -15c against a 1c edge.  Improving the bid by a tick buys
    # queue priority -- and since the tick and the spread are both 1c, it hands
    # over the whole gross edge to get it.  This arm measures whether the front
    # of the queue at least escapes the adverse selection, which decides whether
    # making is merely unprofitable or actively picked off at every position.
    if bid is not None and st.cfg.maker_improve:
        bid = round(bid + 0.01 * st.cfg.maker_improve, 4)
        qsz = 0.0
    # Improving the bid costs a tick, so what is left to capture is
    # (spread/2 - tick).  Section 63 read that as a flat -0.5c from five fills
    # that all happened to be on a 1c spread; the hourly book is 1c only half
    # the time, is 2c a fifth, and is 3c or wider a quarter -- where a
    # one-tick improvement still captures +0.5c to +3.0c.  Gate on the capture
    # actually available rather than assuming the spread.
    if bid is not None and mid - bid < st.cfg.maker_min_capture:
        st.rest.pop(tok, None)
        return
    if (bid is None or not st.cfg.min_ask <= mid <= st.cfg.max_price
            or snap["tau"] <= st.cfg.min_tau_open or st.halted):
        st.rest.pop(tok, None)
        return
    o = st.rest.get(tok)
    if o is None:
        st.rest[tok] = dict(price=bid, ahead=qsz, sold=0.0, born=now)
        return
    # An order does NOT die because someone else bid higher.  It stops being
    # the best bid and still fills if the price comes back.  queuecheck.py v1
    # and v2 made exactly this mistake -- closing a level when the best-bid
    # PRICE changed -- and measured level lifetimes of 0.04s and a fill rate
    # near zero, both artifacts of the clock.  The first maker_step reproduced
    # it and took zero fills across 2,054 in-band quotes.  Only cancel when a
    # real maker would: the price has drifted too far from the mid to be worth
    # holding.
    if abs(mid - o["price"]) > st.cfg.maker_cancel:
        st.rest.pop(tok, None)
        return
    # `sold > 0` is not redundant: with --maker-improve the queue ahead is
    # empty, and `sold >= ahead` is then satisfied at sold = 0 -- the order
    # fills before any trade has happened.  The first run of the front-of-queue
    # arm took four such phantom fills in 90 seconds.  A fill requires somebody
    # to actually sell to you, at every queue position.
    if o["sold"] <= 0 or o["sold"] < o["ahead"]:
        return
    st.rest.pop(tok, None)
    room = min(st.cfg.max_per_market - st.held(slug, side)[1],
               free_capital(st))
    if room <= 0:
        return
    sz = min(st.cfg.max_usd, room) / bid
    if sz > 0:
        log(st, "open", slug, snap, side, bid, qsz, mid - bid, sz, fee=0.0)


def free_capital(st):
    """Spendable dollars, honouring the simultaneous-exposure cap.

    A drawdown limit only stops OPENING.  It cannot stop capital already
    committed from settling against you, so the realised drawdown overshoots the
    limit by whatever was at risk when it fired.  paper_dog5 halted correctly at
    50.7% and still reached 59.4%, and had its $39.23 of open exposure settled
    to zero it would have reached 84% -- from a limit set at 50%.

    Bounding simultaneous exposure is what bounds that overshoot, so it is a
    separate control from the drawdown limit rather than a refinement of it.
    """
    cap = st.equity * st.cfg.max_committed if st.cfg.max_committed > 0 else st.equity
    return max(min(st.equity, cap) - st.committed, 0.0)


def kelly_usd(p, ask, bankroll, frac):
    """Fractional Kelly stake for a binary paying $1 at price `ask`.

    Net odds b = (1-ask)/ask, so f* = (p*(1+b) - 1)/b collapses to
    (p - ask)/(1 - ask).  Full Kelly is far too hot when p is a model output
    rather than a known probability -- a small bias in p blows the account up
    -- so this is always scaled by `frac`.
    """
    if not (0.0 < ask < 1.0) or p <= ask:
        return 0.0
    return max((p - ask) / (1.0 - ask) * frac * bankroll, 0.0)


def cap_shares(st, slug, side):
    """Never buy more of the hedge than there are unmatched shares to cover."""
    other = "Down" if side == "Up" else "Up"
    return st.unmatched(slug, other)


async def strategy(st):
    last = {}
    while True:
        now = time.time()
        for slug, mk in list(st.markets.items()):
            # strike = the settlement source's value at the window open.  That
            # source is a twap-60s stream, so the value it publishes on the
            # boundary is the mean of the preceding minute, not the tick on it.
            # Sampling spot here was wrong by a minute of drift, and because
            # the model's variance collapses as tau^3 that error came back as
            # confident, losing trades.
            # Hourly markets settle on the Binance 1h candle, close versus open,
            # so the strike is read exactly rather than reconstructed. The three
            # largest error sources in the 5-minute case -- a 5.3bps venue
            # basis, the TWAP approximation and a strike integrated from 60s of
            # ticks -- are all identically zero here.
            if st.cfg.hourly:
                if mk["strike"] is None and now > mk["start"] + 2:
                    op = kline_open(BN[mk["asset"]], mk["start"])
                    if op:
                        mk["strike"] = op
                        st.db.execute(
                            "INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?,?,NULL)",
                            (slug, mk["asset"], mk["start"], mk["up"], mk["dn"], op))
                        st.db.commit()
            elif mk["strike"] is None and mk["start"] <= now < mk["start"] + 2:
                i0 = st.integral(mk["asset"], mk["start"] - TWAP_W, mk["start"])
                if i0:
                    mk["strike"] = i0 / TWAP_W
                    st.db.execute(
                        "INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?,?,NULL)",
                        (slug, mk["asset"], mk["start"], mk["up"], mk["dn"],
                         mk["strike"]))
                    k1 = st.kl[mk["asset"]].get("1m") or []
                    k5 = st.kl[mk["asset"]].get("5m") or []
                    mk["er1"], mk["path1"] = regime_of(k1[-30:])
                    er5, _ = regime_of(k5[-12:])
                    rng = (max(k1[-30:]) - min(k1[-30:])) / mk["strike"] * 1e4 \
                        if len(k1) >= 30 else None
                    st.db.execute("INSERT OR REPLACE INTO regime VALUES(?,?,?,?,?)",
                                  (slug, mk["er1"], er5, rng, mk["path1"]))
                    st.db.commit()

            k, tau = mk["strike"], mk["start"] + st.cfg.window - now
            if k is None or not (0 < tau < st.cfg.window):
                continue
            spot = st.spot(mk["asset"])
            if spot is None:
                continue

            sig_tr = fair.realized_sigma(list(st.ticks[mk["asset"]]))
            r_cnt, r_not, tps = st.intensity(mk["asset"])
            # Trailing sigma answers "how volatile was it"; pricing needs "how
            # volatile will the remaining tau be".  Trade intensity is the
            # standard bridge: variance per unit time tracks the arrival rate,
            # so sigma scales with its square root.  Clipped, because a thin
            # baseline can make the ratio explode.
            scale = min(max(r_cnt ** 0.5, 0.6), 2.0) if st.cfg.vol_scale else 1.0
            sigma = sig_tr * scale
            w = st.cfg.twap_w
            ik = st.integral(mk["asset"], mk["start"] + st.cfg.window - w, now) or 0.0 \
                if tau < w else 0.0
            p_up = fair.fair_up(spot, k, tau, sigma, w, ik)
            naive = fair.fair_up_naive(spot, k, tau, sigma)
            snap = dict(tau=tau, spot=spot, strike=k, sigma=sigma,
                        fair_up=p_up, naive_up=naive, intens=r_not, tps=tps)

            a_up, d_up = st.books.get(mk["up"], Book()).best_ask()
            a_dn, d_dn = st.books.get(mk["dn"], Book()).best_ask()

            # Gamma-scalp edge condition: the trade pays roughly
            # (realised vol - implied vol), so record what the book itself is
            # pricing rather than only what the tape did.
            if a_up and a_dn:
                snap["isig"] = fair.implied_sigma(
                    (a_up + 1.0 - a_dn) / 2.0, spot, k, tau, w, ik)

            # How much would both sides cost RIGHT NOW, fees included?  They are
            # complementary tokens, so this should never be under a dollar.
            # Recorded rather than assumed.
            if a_up and a_dn:
                cost = a_up + a_dn + fair.taker_fee(a_up) + fair.taker_fee(a_dn)
                st.db.execute("INSERT INTO pairs VALUES(?,?,?,?,?,?,?,?)",
                              (now, slug, tau, a_up, a_dn, cost, r_not, tps))

            for side, tok, p, ask, depth in (
                    ("Up", mk["up"], p_up, a_up, d_up),
                    ("Down", mk["dn"], 1 - p_up, a_dn, d_dn)):
                if ask is None or depth <= 0 or (slug, side) in st.pending:
                    continue
                # Refuse a quote that has not moved recently: it is a stale
                # snapshot, not a price anyone will fill.
                # A quote nobody has touched is what --max-stale was built to
                # refuse, on the grounds that a frozen book is not a tradeable
                # price.  But the account this project started from pays
                # $46,573 of TAKER fees and still clears 1.64c a share gross,
                # and no calibrated quote offers that -- so stale quotes are
                # the only candidate left.  Record the age and let the trading
                # gate decide, rather than discard the observation unmeasured.
                age = st.books[tok].stale_for(now)
                if age > st.cfg.max_stale and not st.cfg.log_stale:
                    continue
                # --log-stale only widens what is RECORDED.  Every path that
                # spends money still checks `fresh`, so the guard that fixed
                # six findings is not quietly removed to run this experiment.
                fresh = age <= st.cfg.max_stale

                # --- favourite-only (1) or underdog-only (-1).  Near the money
                # both asks sit above 0.50 -- they sum to about $1.035 -- so a
                # price threshold cannot name a side and buys both.  The
                # complement's quote can: the favourite is simply the dearer of
                # the two at this instant.
                if st.cfg.fav_only and a_up and a_dn:
                    if (ask - (a_dn if side == "Up" else a_up)) * st.cfg.fav_only <= 0:
                        continue

                if st.cfg.maker:
                    if not fresh or not a_up or not a_dn:
                        continue          # no mid without both sides quoted
                    m_up = (a_up + (1.0 - a_dn)) / 2.0
                    maker_step(st, slug, side, tok, snap, now,
                               m_up if side == "Up" else 1.0 - m_up)
                    if now - last.get((slug, side), 0) > 20:
                        last[(slug, side)] = now
                        log(st, "sample", slug, snap, side, ask, depth,
                            fair.edge(p, ask), stale=age)
                    continue

                # --- confidence haircut: the random-walk null says the book is
                # the consensus estimate and our p is one noisy model's opinion.
                # Shrinking toward the book's own implied price before sizing
                # keeps a mis-estimated sigma from turning into a large bet.
                mkt = (a_up + (1.0 - a_dn)) / 2.0 if (a_up and a_dn) else p
                if side == "Down":
                    mkt = 1.0 - mkt
                p = fair.shrink(p, mkt, st.cfg.shrink)
                snap["fair"] = p

                # --- can a hedge realistically appear before the window shuts?
                # A lock needs the opposite side to fall to a_max.  Translate
                # that into the spot distance required, then price the odds of
                # travelling it with the reflection principle.  Legs opened when
                # this is near zero are naked bets wearing a hedge's clothes.
                a_max = 1.0 - (ask + fair.taker_fee(ask)) - st.cfg.min_lock
                if a_max <= 0:
                    p_lock = 0.0
                else:
                    dist = fair.needed_move(p, 1.0 - a_max, sigma, tau, w)
                    p_lock = fair.p_touch(dist, sigma, tau)

                # --- lock: we are long the other side, can we finish the pair
                # under a dollar?  Allowed regardless of what the model thinks,
                # because completing it is a guaranteed payout.
                need = cap_shares(st, slug, side)
                if need > 0:
                    other = "Down" if side == "Up" else "Up"
                    basis = st.fifo_basis(slug, other)
                    if basis is None:
                        continue
                    # Holding the open leg is worth `p_other` per share; locking
                    # is worth 1 - basis - ask - fee.  Locking therefore only
                    # beats holding when 1 - ask - fee > p_other, i.e. when the
                    # hedge leg is itself a positive-edge trade.  The old rule
                    # only asked whether the pair came in under $1, which fires
                    # on winners too and flattens a 74%-accurate signal into a
                    # flat few percent.
                    band = (fair.hedge_band(ask, p) if st.cfg.adaptive_band
                            else st.cfg.min_lock)
                    snap["band"] = band
                    cheap = 1.0 - basis - ask - fair.taker_fee(ask) >= band
                    plus = fair.edge(p, ask) > st.cfg.min_edge
                    want = {"cost": cheap, "edge": plus,
                            "both": cheap and plus}[st.cfg.lock_mode]
                    if want and fresh:
                        st.pending.add((slug, side))
                        asyncio.create_task(
                            fill(st, "lock", slug, side, tok, dict(snap), basis))
                        continue

                # --- open: directional, and only with enough of the window
                # left for the price to move far enough to lock it.
                ed = fair.edge(p, ask)
                # The whole model measured against one comparison. Buying the
                # side spot sits on beats the TWAP model's own edge filter on
                # every count: +12.95c/share (t 5.51) against +12.02c (t 2.33)
                # in the last minute, and it trades twice as often. Direction
                # is where the model is right -- it agrees with spot in 2,180
                # of 2,181 samples -- and confidence is where it is wrong, so
                # filtering on edge discards the good trades along with the
                # bad. Kept as a mode so the two run head to head.
                if st.cfg.spot_rule:
                    # Distance matters as much as direction. Under 2bps from the
                    # strike the rule is noise (73.5% win, t 0.47); past 5bps it
                    # has not lost yet across 23 markets. "Has not lost yet" is
                    # the point: a rule with no observed losses has an
                    # UNMEASURED loss distribution, and its t is high precisely
                    # because the variance estimate is zero rather than small.
                    # Mechanically it must break -- 5bps on BTC is $3.85 while a
                    # 60-second move is around $23 -- so this is sized as a thin
                    # edge, not a certainty.
                    gap = abs(spot - k) / max(k, 1e-9) * 1e4
                    ok = gap >= st.cfg.min_gap_bps
                    ed = 0.02 if (ok and side == ("Up" if spot > k else "Down")) else -1.0
                # Variance risk premium gate. Measured on 156 settled markets
                # and replicated independently in both arms (r -0.28 / -0.38,
                # both past their own 95% band): the trade pays when the book
                # prices MORE uncertainty than the tape is delivering, and
                # loses when it prices less. That is the short-dated variance
                # risk premium, and it is the same phenomenon as every leg
                # bought under $0.20 settling worthless -- a book that is more
                # confident than reality makes longshots look cheap when they
                # are not. Unlike trend or intensity this is per-observation,
                # not per-window, which is why it replicates.
                isig = snap.get("isig")
                vrp_ok = (st.cfg.min_vrp <= 0 or
                          (isig and sigma > 0 and isig / sigma >= st.cfg.min_vrp))
                # Filters chosen by measuring which features separate legs
                # that got hedged from legs that went naked (681 legs, AUC):
                #   ask 0.768  -- buy the side the book already favours; a
                #                 coin-flip leg has no drift to open a hedge
                #   depth 0.348 (inverted) -- a thin best ask is a quote about
                #                 to move; a deep one is someone who means it
                #   intensity 0.376 (inverted) -- a busy tape means makers
                #                 widen and the stale-quote edge is gone
                room = st.cfg.max_per_market - st.held(slug, side)[1]
                budget = free_capital(st)
                if (fresh and ed > st.cfg.min_edge and ask <= st.cfg.max_price
                        and ask >= st.cfg.min_ask
                        and depth <= st.cfg.max_depth
                        and vrp_ok
                        and r_not <= st.cfg.max_intensity
                        and budget > st.cfg.min_usd
                        and not st.halted
                        and room > 0 and tau > st.cfg.min_tau_open
                        and r_not >= st.cfg.min_intensity
                        and st.cfg.er_min <= (mk.get("er1") or 0.5)
                                          <= st.cfg.er_max
                        and (mk.get("path1") or 99) >= st.cfg.path_min
                        and p_lock >= st.cfg.min_p_lock):
                    st.pending.add((slug, side))
                    asyncio.create_task(
                        fill(st, "open", slug, side, tok, dict(snap), 0.0))

                key = (slug, side)
                if now - last.get(key, 0) > 20:
                    last[key] = now
                    log(st, "sample", slug, snap, side, ask, depth, ed,
                        stale=age)
        st.db.commit()
        await asyncio.sleep(st.cfg.tick_ms / 1000.0)


async def heartbeat(st):
    while True:
        await asyncio.sleep(60)
        q = lambda s: st.db.execute(s).fetchone()[0]
        print(f"[hb] mk={len(st.markets)} tok={len(st.wanted)} "
              f"open={q('SELECT COUNT(*) FROM obs WHERE kind=\"open\"')} "
              f"lock={q('SELECT COUNT(*) FROM obs WHERE kind=\"lock\"')} "
              f"res={q('SELECT COUNT(*) FROM markets WHERE outcome IS NOT NULL')} "
              f"eq=${st.equity:,.2f} used=${st.committed:,.2f}"
              f"{' HALTED' if st.halted else ''}",
              flush=True)


async def main(a):
    st = State(a)
    jobs = [feed_binance(st), feed_books(st), discover(st), resolve(st),
            strategy(st), heartbeat(st)]
    if not a.no_coinbase:
        jobs.append(feed_coinbase(st))
    if not a.no_chainlink:
        jobs.append(poll_chainlink(st))
    jobs.append(poll_klines(st))
    await asyncio.gather(*jobs)


# ---------------------------------------------------------------- report
def report(a):
    import math
    db = sqlite3.connect(a.db)
    res = dict(db.execute(
        "SELECT slug, outcome FROM markets WHERE outcome IS NOT NULL").fetchall())
    print(f"resolved markets: {len(res)}")
    if not res:
        return print("nothing resolved yet -- let it run longer")

    rows = [r for r in db.execute(
        "SELECT slug,kind,side,fair_up,naive_up,fill_px,fill_sz,fee FROM obs")
        if r[0] in res]

    for name, col in (("twap ", 3), ("naive", 4)):
        ll = n = 0
        for r in rows:
            if r[1] != "sample":
                continue
            p = min(max(r[col], 1e-6), 1 - 1e-6)
            y = 1.0 if res[r[0]] == "Up" else 0.0
            ll -= y * math.log(p) + (1 - y) * math.log(1 - p)
            n += 1
        print(f"{name} log-loss {ll/max(n,1):.4f}  over {n:,} samples")

    print("\ncalibration (twap model, Up leg)")
    bk = {}
    for r in rows:
        if r[1] != "sample":
            continue
        b = min(int(r[3] * 10), 9)
        h, t = bk.get(b, (0, 0))
        bk[b] = (h + (res[r[0]] == "Up"), t + 1)
    for b in sorted(bk):
        h, t = bk[b]
        print(f"  p {b/10:.1f}-{b/10+0.1:.1f}  n={t:>5,}  actual={h/t:.3f}"
              f"{'   OVER' if h/t < b/10 else ''}")

    # --- position level P&L: matched pairs pay exactly $1, the rest is naked
    pos = {}
    for slug, kind, side, *_ , px, sz, fee in rows:
        if kind == "sample" or not sz:
            continue
        p = pos.setdefault(slug, {"Up": [0.0, 0.0], "Down": [0.0, 0.0],
                                  "nlock": 0, "nopen": 0})
        p[side][0] += sz
        p[side][1] += px * sz + fee
        p["nlock" if kind == "lock" else "nopen"] += 1

    # Pairs come from the `matched` ledger, written at lock time against the
    # exact FIFO lots consumed.  Recomputing them from average cost lets later
    # opens leak backwards into already-hedged shares.
    mt = {}
    for slug, sh, pair in db.execute(
            "SELECT slug, shares, pair FROM matched"):
        if slug in res:
            e = mt.setdefault(slug, [0.0, 0.0])
            e[0] += sh
            e[1] += pair

    tot = dict(pair_n=0, pair_cost=0.0, pair_pay=0.0, naked_cost=0.0,
               naked_pay=0.0, naked_n=0, win=0, mkts=0, opens=0, locks=0)
    for slug, p in pos.items():
        tot["mkts"] += 1
        tot["opens"] += p["nopen"]
        tot["locks"] += p["nlock"]
        m, cost = mt.get(slug, [0.0, 0.0])
        if m > 0:                                  # hedged pairs -> $1 each
            tot["pair_n"] += m
            tot["pair_cost"] += cost
            tot["pair_pay"] += m
        # The residue is what is LEFT, so take it by subtraction.  Pricing it
        # at blended average cost instead mixes two costing bases -- the pairs
        # come from the FIFO `matched` ledger, and the lots it consumed are not
        # the average -- and the split then fails to add back up to the money
        # actually spent.  It was short by $46 on a $797 book before this.
        tot["naked_cost"] += p["Up"][1] + p["Down"][1] - cost
        tot["naked_pay"] += p[res[slug]][0] - m
        for side in ("Up", "Down"):
            if p[side][0] - m > 1e-9:
                tot["naked_n"] += 1
                if res[slug] == side:
                    tot["win"] += 1

    pp = tot["pair_pay"] - tot["pair_cost"]
    np_ = tot["naked_pay"] - tot["naked_cost"]
    print(f"\nmarkets traded {tot['mkts']}   open legs {tot['opens']}   "
          f"lock legs {tot['locks']}")
    print(f"  hedged pairs  {tot['pair_n']:>9,.1f} sh  cost ${tot['pair_cost']:>9,.2f}"
          f"  payout ${tot['pair_pay']:>9,.2f}  P&L ${pp:>+9,.2f}")
    if tot["pair_n"]:
        print(f"                cost per $1 pair = ${tot['pair_cost']/tot['pair_n']:.4f}"
              f"   (under 1.0000 = locked profit)")
    print(f"  naked residue {tot['naked_n']:>9} legs  cost ${tot['naked_cost']:>9,.2f}"
          f"  payout ${tot['naked_pay']:>9,.2f}  P&L ${np_:>+9,.2f}"
          f"   win {tot['win']}/{tot['naked_n']}")
    stake = tot["pair_cost"] + tot["naked_cost"]
    print(f"  TOTAL         stake ${stake:,.2f}   NET ${pp+np_:>+,.2f}"
          f"   ({(pp+np_)/max(stake,1e-9)*100:+.2f}%)")

    # Counterfactual: hold every directional leg to settlement and never hedge.
    # Locking caps a winner at a few percent while a loser still goes to zero,
    # so this is the number that says whether hedging is paying for itself.
    c_cost = c_pay = 0.0
    hit = shot = 0
    hsh = lsh = 0.0
    for slug, kind, side, *_ , px, sz, fee in rows:
        if kind != "open" or not sz:
            continue
        c_cost += px * sz + fee
        shot += 1
        if res[slug] == side:
            c_pay += sz
            hit += 1
            hsh += sz
        else:
            lsh += sz
    if shot:
        print(f"\ncounterfactual -- same open legs, never hedged:")
        print(f"  direction right {hit}/{shot} legs"
              f"  ({hsh:,.0f} winning shares vs {lsh:,.0f} losing)")
        print(f"  stake ${c_cost:,.2f}  payout ${c_pay:,.2f}"
              f"  NET ${c_pay-c_cost:>+,.2f}"
              f"  ({(c_pay-c_cost)/max(c_cost,1e-9)*100:+.2f}%)")
        print(f"  -> hedging changed the result by "
              f"${(pp+np_)-(c_pay-c_cost):+,.2f}")

    # Every share settles at exactly 0 or 1, so regrouping the same legs into
    # pairs + residue MUST reproduce open legs + hedge legs.  This ran nested
    # inside another block for a while and silently stopped checking anything
    # on arms that never hedge -- which are exactly the arms it matters for.
    # It stays at top level and unconditional.
    def _identity():
        gap = (pp + np_) - ((c_pay - c_cost) + (l_pay - l_cost))
        print(f"\nidentity check: pairs+residue - (opens+hedges) = ${gap:+.4f}"
              f"   {'OK' if abs(gap) < 0.01 else '<<< BROKEN'}")

    # --- is the hedge leg itself a good purchase?
    # Section 5 established the sequential lock is not arbitrage: it is the
    # winning branch of a directional bet.  It can still ADD value, but only if
    # the hedge leg is bought at a price that is +EV on its own -- that is what
    # `--lock-mode edge` tries to require.  Whether the pair came in under $1 is
    # a different question and does not answer this one.
    lk = {}
    for slug, kind, side, *_ , px, sz, fee in rows:
        if kind != "lock" or not sz:
            continue
        e = lk.setdefault(slug, [0.0, 0.0, 0, 0])
        e[0] += px * sz + fee
        e[3] += 1
        if res[slug] == side:
            e[1] += sz
            e[2] += 1
    l_cost = l_pay = 0.0
    if lk:
        l_cost = sum(v[0] for v in lk.values())
        l_pay = sum(v[1] for v in lk.values())
        l_won = sum(v[2] for v in lk.values())
        l_legs = sum(v[3] for v in lk.values())
        per_m = [v[1] - v[0] for v in lk.values()]
        mu = statistics.mean(per_m)
        sd = statistics.stdev(per_m) if len(per_m) > 1 else 0.0
        tt = mu / (sd / math.sqrt(len(per_m))) if sd else 0.0
        print(f"\nhedge leg as a standalone bet"
              f"  ({l_legs} legs over {len(lk)} markets):")
        print(f"  stake ${l_cost:,.2f}  payout ${l_pay:,.2f}"
              f"  NET ${l_pay-l_cost:>+,.2f}"
              f"  ({(l_pay-l_cost)/max(l_cost,1e-9)*100:+.2f}%)"
              f"   won {l_won}/{l_legs}")
        print(f"  per market ${mu:+.4f}  sd ${sd:.4f}  t={tt:+.2f}"
              f"   (needs |t|>2.64 under the six-arm Bonferroni)")

    # --- does predicted edge predict anything?
    # Section 40 collapsed the strategy to two directional bets, so the only
    # lever left is whether the model's edge at entry is real.  This is a
    # calibration test on the quantity that actually drives sizing: bucket
    # every traded leg by predicted edge per share and compare against what
    # the share paid.  A working model slopes up and sits near the diagonal.
    # t is clustered at market level -- legs inside one market share an
    # outcome and are not independent observations.
    eb = {}
    for slug, side, ed, px, sz, fee in db.execute(
            "SELECT slug,side,edge,fill_px,fill_sz,fee FROM obs"
            " WHERE fill_sz IS NOT NULL AND edge IS NOT NULL"):
        if slug not in res or not sz:
            continue
        b = min(int(ed * 100 // 2), 6)             # 2c buckets, 12c+ pooled
        real = ((1.0 if res[slug] == side else 0.0) - px) * sz - fee
        e = eb.setdefault(b, [0, 0.0, 0.0, 0.0, {}])
        e[0] += 1
        e[1] += sz
        e[2] += ed * sz
        e[3] += real
        e[4][slug] = e[4].get(slug, 0.0) + real
    # --- is the BOOK calibrated?  This is the question underneath all of it.
    # Traded legs are selected on the model's opinion, so a gradient across them
    # confounds the book with the filter that picked them.  Sample rows are the
    # book quoted on a fixed schedule regardless of what the model thought, so
    # they measure the market itself.  Prediction markets classically overprice
    # longshots; if that holds here it is an edge that owes nothing to the model
    # -- and the taker fee of 7%x(1-p) of stake is what it has to clear.
    bb = {}
    for slug, side, ask in db.execute(
            "SELECT slug,side,ask FROM obs WHERE kind='sample' AND ask IS NOT NULL"):
        if slug not in res:
            continue
        b = min(int(ask * 10), 9)
        e = bb.setdefault(b, [0, 0.0, 0.0, {}])
        w = 1.0 if res[slug] == side else 0.0
        e[0] += 1; e[1] += ask; e[2] += w
        m = e[3].setdefault(slug, [0, 0.0])
        m[0] += 1; m[1] += w - ask - fair.taker_fee(ask)
    if bb:
        print("\nbook calibration on sample quotes (all, not just traded)")
        print(f"  {'ask':<10}{'n':>7}{'mkts':>6}{'mean ask':>10}"
              f"{'won':>8}{'net/sh':>9}{'+50% rb':>9}{'t':>8}")
        for b in sorted(bb):
            n, sa, sw, per = bb[b]
            v = [x[1] / x[0] for x in per.values()]
            sd = statistics.stdev(v) if len(v) > 1 else 0.0
            tt = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
            fe = fair.taker_fee(sa / n)
            net = sw / n - sa / n - fe
            # The rebate is a refund of the fee capped at 50% (Obsidian tier),
            # never money on top, so net + fee/2 is the ceiling for any taker
            # at any volume.  It shifts every row up by half of 7%p(1-p) and
            # changes no sign that matters.
            print(f"  {f'{b/10:.1f}-{b/10+0.1:.1f}':<10}{n:>7,}{len(per):>6}"
                  f"{sa/n:>10.3f}{sw/n:>8.3f}{net:>+9.4f}"
                  f"{net + fe / 2:>+9.4f}{tt:>+8.2f}")

    starts = dict(db.execute(
        "SELECT slug, start_ts FROM markets WHERE outcome IS NOT NULL"))
    # --- near the money, does the book shrink toward 0.5?
    # The 0.4-0.5 and 0.5-0.6 calibration cells miss in opposite directions,
    # which is one claim seen from both sides: the favourite wins more than its
    # price implies.  Two traps sank the first two attempts at this.
    #   (1) "ask > 0.5" does not identify a side.  The two asks sum to about
    #       $1.035, so near the money BOTH sit above 0.50 and the same instant
    #       gets counted as two favourites.  Fixing it with a 0.54 floor
    #       instead threw away the genuine mild favourites and the effect went
    #       with them.  `pairs` quotes both sides at one instant, so the
    #       favourite is simply the dearer one -- no threshold needed.
    #   (2) btc, eth and sol resolve the same five minutes of the same risk
    #       asset.  Three markets per window is one observation wearing three
    #       hats; clustering per market inflates t by about sqrt(3).
    # Staleness is the remaining worry, and the tau split is what separates it:
    # a slow book looks exactly like an under-confident one, but only near
    # expiry.
    sk = {}
    for slug, tau, au, ad in db.execute(
            "SELECT slug,tau,ask_up,ask_dn FROM pairs WHERE tau IS NOT NULL"):
        if slug not in res or au is None or ad is None:
            continue
        side, px = ("Up", au) if au > ad else ("Down", ad)
        if px > 0.65:
            continue
        k = 0 if tau < 30 else (1 if tau < 90 else 2)
        m = sk.setdefault(k, {}).setdefault(starts.get(slug, slug), [0, 0.0, 0.0])
        m[0] += 1
        m[1] += (1.0 if res[slug] == side else 0.0) - px
        m[2] += (1.0 if res[slug] == side else 0.0) - px - fair.taker_fee(px)
    if sk:
        print("\nfavourite (dearer side) vs its own price, by time to expiry")
        print(f"  {'tau':<10}{'quotes':>9}{'windows':>9}"
              f"{'too timid':>12}{'net/sh':>10}{'t':>8}")
        for k in sorted(sk):
            v = [x[1] / x[0] for x in sk[k].values()]
            nt = [x[2] / x[0] for x in sk[k].values()]
            if len(v) < 5:
                continue
            sd = statistics.stdev(v) if len(v) > 1 else 0.0
            tt = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
            print(f"  {('< 30s','30-90s','> 90s')[k]:<10}"
                  f"{sum(x[0] for x in sk[k].values()):>9,}{len(v):>9}"
                  f"{statistics.mean(v):>+12.4f}{statistics.mean(nt):>+10.4f}"
                  f"{tt:>+8.2f}")

    # --- is a stale quote a mispricing or a mirage?
    # The guard that refuses quotes nobody has touched fixed six findings, on
    # the reasoning that a frozen book is not a price anyone will fill.  The
    # account this project started from contradicts that: it pays $46,573 of
    # taker fees -- so it crosses -- and still clears 1.64c a share gross,
    # which no calibrated quote offers.  Stale quotes are the only candidate
    # left.  Needs `--log-stale 1`; older databases have no rows here.
    sq = {}
    # `report()` opens the file directly and never builds a State, so it does
    # not get State's ALTER TABLE.  Databases written before --log-stale have no
    # such column, and asking for it took the whole report down -- which is the
    # one path every P&L conclusion here is allowed to come from.
    have_stale = "stale" in {r[1] for r in db.execute("PRAGMA table_info(obs)")}
    for slug, side, ask, age in (db.execute(
            "SELECT slug,side,ask,stale FROM obs WHERE kind='sample'"
            " AND ask IS NOT NULL AND stale IS NOT NULL") if have_stale else []):
        if slug not in res:
            continue
        b = 0 if age < 5 else (1 if age < 20 else (2 if age < 60 else 3))
        e = sq.setdefault(b, {}).setdefault(starts.get(slug, slug), [0, 0.0])
        e[0] += 1
        e[1] += (1.0 if res[slug] == side else 0.0) - ask - fair.taker_fee(ask)
    if sq:
        print("\ntaker net by how long the quote has sat untouched")
        print(f"  {'age':<12}{'quotes':>9}{'windows':>9}{'net/sh':>10}{'t':>8}")
        for b in sorted(sq):
            per = sq[b]
            v = [x[1] / x[0] for x in per.values()]
            if len(v) < 5:
                continue
            sd = statistics.stdev(v) if len(v) > 1 else 0.0
            t = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
            print(f"  {('< 5s','5-20s','20-60s','> 60s')[b]:<12}"
                  f"{sum(x[0] for x in per.values()):>9,}{len(v):>9}"
                  f"{statistics.mean(v):>+10.4f}{t:>+8.2f}")

    print("\nadverse-selection exposure of a resting order")
    # --- how far does the mid travel while an order rests?
    # A maker's gross edge is the half-spread and nothing else.  It survives only
    # if the mid moves less than that between posting and filling, and on these
    # markets the informed trader is anyone with a Binance feed -- which is
    # everyone.  This is the size of the gun pointed at a resting bid.
    seq = {}
    for slug, ts, au, ad in db.execute(
            "SELECT slug,ts,ask_up,ask_dn FROM pairs ORDER BY slug, ts"):
        if not au or not ad:
            continue
        seq.setdefault(slug, []).append((ts, (au + 1.0 - ad) / 2.0))
    for h in (10.0, 30.0, 60.0):
        mv = []
        for pts in seq.values():
            j = 0
            for i, (t, m) in enumerate(pts):
                while j < len(pts) and pts[j][0] < t + h:
                    j += 1
                if j >= len(pts):
                    break
                mv.append(abs(pts[j][1] - m))
        if len(mv) > 100:
            mv.sort()
            print(f"  mid travel in {h:>4.0f}s:  median {mv[len(mv)//2]*100:5.2f}c"
                  f"   p75 {mv[int(len(mv)*.75)]*100:5.2f}c"
                  f"   p90 {mv[int(len(mv)*.9)]*100:5.2f}c   n={len(mv):,}")

    # --- is the edge in the book's belief, or only inside the spread?
    # Section 44 found real momentum in the price path but could not say whether
    # the book misses it.  These are two different questions and the spread is
    # what separates them.  The book's belief about Up is the mid,
    # (ask_up + 1 - ask_dn)/2; what a taker actually pays is the ask.  If the
    # outcome beats the mid, the book is wrong.  If it beats the mid but not the
    # ask, the book is wrong by less than the spread and only a maker can
    # collect it.
    fb = {}
    for slug, tau, au, ad in db.execute(
            "SELECT slug,tau,ask_up,ask_dn FROM pairs WHERE tau IS NOT NULL"):
        if slug not in res or not au or not ad:
            continue
        m_up = (au + 1.0 - ad) / 2.0
        side = "Up" if m_up >= 0.5 else "Down"
        mid = m_up if side == "Up" else 1.0 - m_up
        ask = au if side == "Up" else ad
        b = min(int(round((mid - 0.5) * 100)) // 5, 4)   # 5c bands
        e = fb.setdefault(b, {}).setdefault(starts.get(slug, slug), [0, 0.0, 0.0, 0.0])
        y = 1.0 if res[slug] == side else 0.0
        e[0] += 1
        e[1] += y - mid                                  # book wrong?
        e[2] += y - ask - fair.taker_fee(ask)            # taker net
        e[3] += ask - mid                                # half-spread paid
    if fb:
        print("\nfavourite by book MID: is the book wrong, or is it the spread?")
        print(f"  {'mid':<12}{'quotes':>9}{'windows':>8}{'vs mid':>9}{'t':>7}"
              f"{'half-spd':>10}{'taker net':>11}{'t':>7}")
        for b in sorted(fb):
            per = fb[b]
            vm = [x[1] / x[0] for x in per.values()]
            vt = [x[2] / x[0] for x in per.values()]
            hs = [x[3] / x[0] for x in per.values()]
            if len(vm) < 5:
                continue
            sm = statistics.stdev(vm) if len(vm) > 1 else 0.0
            st_ = statistics.stdev(vt) if len(vt) > 1 else 0.0
            tm = statistics.mean(vm) / (sm / math.sqrt(len(vm))) if sm else 0.0
            tt = statistics.mean(vt) / (st_ / math.sqrt(len(vt))) if st_ else 0.0
            lbl = f"{0.5+b*0.05:.2f}-{0.55+b*0.05:.2f}" if b < 4 else "0.70+"
            print(f"  {lbl:<12}{sum(x[0] for x in per.values()):>9,}{len(vm):>8}"
                  f"{statistics.mean(vm):>+9.4f}{tm:>+7.2f}"
                  f"{statistics.mean(hs):>+10.4f}"
                  f"{statistics.mean(vt):>+11.4f}{tt:>+7.2f}")
        # Five buckets all leaning the same way is a 1-in-32 sign test, so the
        # split above is not the right unit -- it is five looks at one claim.
        # Pool to one number per window, one test: is the book's mid biased at
        # all, and does anything survive the fee?
        pool = {}
        for per in fb.values():
            for wk, x in per.items():
                e = pool.setdefault(wk, [0, 0.0, 0.0])
                e[0] += x[0]; e[1] += x[1]; e[2] += x[2]
        vm = [x[1] / x[0] for x in pool.values()]
        vt = [x[2] / x[0] for x in pool.values()]
        for lbl, v in (("pooled vs mid", vm), ("pooled taker net", vt)):
            sd = statistics.stdev(v) if len(v) > 1 else 0.0
            t = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
            print(f"  {lbl:<20}{len(v):>5} windows"
                  f"{statistics.mean(v):>+10.4f}   t={t:+.2f}")

    # Same legs, bucketed by price paid instead.  The taker fee is 7%x(1-p) of
    # stake, so it costs 3.5% at the money and 0.14% at 98c: if any edge
    # survives the fee anywhere it is at the extremes, and that is a different
    # question from whether the model ranks edges correctly.
    pb = {}
    for slug, side, px, sz, fee in db.execute(
            "SELECT slug,side,fill_px,fill_sz,fee FROM obs"
            " WHERE fill_sz IS NOT NULL"):
        if slug not in res or not sz:
            continue
        b = min(int(px * 5), 4)                    # 20c bands
        real = ((1.0 if res[slug] == side else 0.0) - px) * sz - fee
        e = pb.setdefault(b, [0, 0.0, 0.0, 0.0, {}])
        e[0] += 1; e[1] += sz; e[2] += fee; e[3] += real
        e[4][slug] = e[4].get(slug, 0.0) + real
    if pb:
        print("\nrealised by price paid (fee as % of stake = 7% x (1-p))")
        print(f"  {'ask':<12}{'legs':>6}{'mkts':>6}{'fee %stk':>10}"
              f"{'real $/sh':>11}{'t':>8}")
        for b in sorted(pb):
            n, sh, fe, rl, per = pb[b]
            v = list(per.values())
            sd = statistics.stdev(v) if len(v) > 1 else 0.0
            tt = statistics.mean(v) / (sd / math.sqrt(len(v))) if sd else 0.0
            print(f"  {f'{b*20}-{b*20+20}c':<12}{n:>6}{len(per):>6}"
                  f"{fe/max(sh,1e-9)*100/max((b*20+10)/100,1e-9):>9.2f}%"
                  f"{rl/max(sh,1e-9):>11.4f}{tt:>+8.2f}")

    if eb:
        print("\npredicted edge vs realised, per traded leg (open + lock)")
        print(f"  {'pred edge':<12}{'legs':>6}{'mkts':>6}"
              f"{'pred $/sh':>11}{'real $/sh':>11}{'t':>8}")
        for b in sorted(eb):
            n, sh, pr, rl, per = eb[b]
            v = list(per.values())
            mu = statistics.mean(v)
            sd = statistics.stdev(v) if len(v) > 1 else 0.0
            tt = mu / (sd / math.sqrt(len(v))) if sd else 0.0
            lbl = f"{b*2}-{b*2+2}c" if b < 6 else "12c+"
            print(f"  {lbl:<12}{n:>6}{len(per):>6}"
                  f"{pr/max(sh,1e-9):>11.4f}{rl/max(sh,1e-9):>11.4f}{tt:>+8.2f}")

    _identity()

    # --- risk: the equity path, rebuilt from the same rows as the P&L above.
    # The `equity` table is per-PROCESS -- it resets to the starting bankroll on
    # every restart and only records markets that settled while that process was
    # alive.  paper_hour showed +$14.34 there against -$17.71 here purely because
    # it was restarted mid-session.  Reading drawdown off that table is how a
    # restarted arm reports a drawdown it never had, so it is rebuilt here from
    # the settled markets in time order instead.
    mp = {}
    for slug, kind, side, *_ , px, sz, fee in rows:
        if kind == "sample" or not sz:
            continue
        mp[slug] = mp.get(slug, 0.0) - (px * sz + fee) \
            + (sz if res[slug] == side else 0.0)
    if mp:
        bank = dict(db.execute("SELECT key,value FROM meta")).get("bankroll") \
            if "meta" in {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")} else None
        if bank is None:
            print(f"\n  [warn] no bankroll recorded in this database; "
                  f"using --bankroll {a.bankroll:g}")
        eq = peak = float(bank if bank is not None else a.bankroll)
        dd = 0.0
        low = eq
        worst = min(mp.values())
        for slug in sorted(mp, key=lambda s: starts.get(s, 0)):
            eq += mp[slug]
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak if peak > 0 else 0.0)
            low = min(low, eq)
        start = float(bank if bank is not None else a.bankroll)
        print(f"\nrisk (rebuilt from settled markets, not the equity table)")
        print(f"  bankroll ${start:,.2f} -> ${eq:,.2f}"
              f"   peak ${peak:,.2f}   trough ${low:,.2f}")
        print(f"  max drawdown {dd*100:.1f}%   worst single market ${worst:+,.2f}"
              f"   markets {len(mp)}")
        if low <= 0:
            print(f"  *** RUINED -- equity reached ${low:,.2f}. "
                  f"Everything after that point is fiction. ***")

    # --- was simultaneous arbitrage ever available?
    r = db.execute("SELECT COUNT(*), MIN(cost), AVG(cost),"
                   " SUM(cost<1.0) FROM pairs").fetchone()
    if r and r[0]:
        print(f"\nsimultaneous two-sided cost over {r[0]:,} quotes:"
              f"  min ${r[1]:.4f}  avg ${r[2]:.4f}")
        print(f"  quotes under $1.00 (true arb): {r[3]:,}"
              f"  ({r[3]/r[0]*100:.3f}%)")

    # --- was a SEQUENTIAL lock available, and does volume predict it?
    # Best achievable pair = cheapest all-in Up seen at any instant plus the
    # cheapest all-in Down seen at any other instant.  Under $1.00 means a
    # perfectly timed leg-in would have locked a profit in that market.
    per = {}
    for slug, au, ad, it in db.execute(
            "SELECT slug, ask_up, ask_dn, intens FROM pairs"):
        e = per.setdefault(slug, [9.9, 9.9, [], 0])
        e[0] = min(e[0], au + fair.taker_fee(au))
        e[1] = min(e[1], ad + fair.taker_fee(ad))
        if it:
            e[2].append(it)
        e[3] += 1
    mk = [(v[0] + v[1], statistics.median(v[2]) if v[2] else 1.0)
          for v in per.values() if v[3] >= 20]
    if mk:
        best = [c for c, _ in mk]
        n_ok = sum(c < 1.0 for c in best)
        print(f"\nsequential lock, {len(mk)} markets with >=20 quotes:")
        print(f"  best achievable pair cost: min ${min(best):.4f}"
              f"  median ${statistics.median(best):.4f}")
        print(f"  markets where a perfectly timed lock beat $1.00:"
              f" {n_ok}/{len(mk)} ({n_ok/len(mk)*100:.1f}%)")
        mk.sort(key=lambda x: x[1])
        third = max(len(mk) // 3, 1)
        for lbl, grp in (("low  volume", mk[:third]), ("high volume", mk[-third:])):
            c = [x[0] for x in grp]
            print(f"  {lbl}: median pair ${statistics.median(c):.4f}"
                  f"  lockable {sum(v<1.0 for v in c)}/{len(c)}"
                  f"  (median intensity {statistics.median([x[1] for x in grp]):.2f})")

    # --- how much of the result is the strategy and how much is the hour?
    # A single favourable stretch can make any directional rule look brilliant.
    # Bucketing by wall-clock hour is the cheapest way to see the spread that a
    # drawdown limit would actually have to survive.
    hourly = {}
    for slug, kind, side, *_ , px, sz, fee in rows:
        if kind != "open" or not sz or slug not in starts:
            continue
        h = time.strftime("%m-%d %H:00", time.gmtime(starts[slug]))
        e = hourly.setdefault(h, [0.0, 0.0, 0, 0, set()])
        e[0] += px * sz + fee
        e[4].add(slug)
        e[3] += 1
        if res[slug] == side:
            e[1] += sz
            e[2] += 1
    if hourly:
        print(f"\nby hour (open legs only, unhedged basis)")
        print(f"  {'hour':<14}{'mkts':>5}{'legs':>6}{'hit':>8}"
              f"{'stake':>10}{'net':>10}{'return':>9}")
        rets = []
        for h in sorted(hourly):
            c, pay, hit, n, mk_ = hourly[h]
            r = (pay - c) / max(c, 1e-9) * 100
            rets.append(r)
            print(f"  {h:<14}{len(mk_):>5}{n:>6}{hit/max(n,1)*100:>7.0f}%"
                  f"{c:>10,.0f}{pay-c:>+10,.0f}{r:>8.1f}%")
        if len(rets) >= 2:
            print(f"  {'':<14}{'':>5}{'':>6}{'':>8}  spread: "
                  f"min {min(rets):+.1f}%  max {max(rets):+.1f}%  "
                  f"stdev {statistics.stdev(rets):.1f} pts")

    # --- which regime actually pays?  Direction and amplitude are separate
    # axes, so split on both rather than on a single "trendiness" number.
    reg = dict((r[0], (r[1], r[2])) for r in
               db.execute("SELECT slug, er1, path1 FROM regime")
               if r[1] is not None and r[2] is not None)
    cf = {}
    for slug, kind, side, *_ , px, sz, fee in rows:
        if kind != "open" or not sz:
            continue
        e = cf.setdefault(slug, [0.0, 0.0])
        e[0] += px * sz + fee
        e[1] += sz if res[slug] == side else 0.0
    live = [(s, reg[s][0], reg[s][1]) for s in cf if s in reg]
    if len(live) >= 4:
        me = statistics.median(x[1] for x in live)
        mp = statistics.median(x[2] for x in live)
        print(f"\nregime grid ({len(live)} markets; split at er={me:.2f},"
              f" path={mp:.0f}bps)")
        print(f"  {'':<22}{'n':>4}{'stake':>10}{'naked P&L':>12}{'return':>9}"
              f"{'pair cost':>11}")
        for lbl, sel in (
                ("chop  / small path", lambda e, p: e <= me and p <= mp),
                ("chop  / long path ", lambda e, p: e <= me and p > mp),
                ("trend / small path", lambda e, p: e > me and p <= mp),
                ("trend / long path ", lambda e, p: e > me and p > mp)):
            g = [s for s, e, p in live if sel(e, p)]
            if not g:
                continue
            c = sum(cf[s][0] for s in g)
            pay = sum(cf[s][1] for s in g)
            pm = [mt[s][1] / mt[s][0] for s in g if s in mt and mt[s][0] > 0]
            print(f"  {lbl:<22}{len(g):>4}{c:>10,.0f}{pay-c:>+12,.2f}"
                  f"{(pay-c)/max(c,1e-9)*100:>8.1f}%"
                  f"{('$%.4f' % statistics.median(pm)) if pm else '-':>11}")

    b = db.execute("SELECT COUNT(*), AVG(bps), MIN(bps), MAX(bps),"
                   " AVG(ABS(bps)) FROM basis").fetchone()
    if b and b[0]:
        print(f"\nconsolidated spot vs on-chain Chainlink, {b[0]:,} samples:"
              f"  mean {b[1]:+.2f} bps  range [{b[2]:+.1f}, {b[3]:+.1f}]"
              f"  mean|basis| {b[4]:.2f} bps")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--assets", default="btc,eth,sol", type=lambda s: s.split(","))
    p.add_argument("--hourly", type=int, default=0,
                   help="trade the 1-hour markets instead of the 5-minute ones; "
                        "they settle on the Binance 1h candle, so the strike is "
                        "exact rather than estimated")
    p.add_argument("--twap-w", type=float, default=60.0)
    p.add_argument("--min-edge", type=float, default=0.01,
                   help="model edge per share, net of fee, to open a leg")
    p.add_argument("--min-lock", type=float, default=0.015,
                   help="locked profit per $1 pair required to hedge")
    p.add_argument("--min-tau-open", type=float, default=45.0,
                   help="no new naked legs inside this many seconds of close")
    p.add_argument("--max-usd", type=float, default=25.0, help="per fill")
    p.add_argument("--max-per-market", type=float, default=60.0)
    p.add_argument("--max-price", type=float, default=0.95,
                   help="never lift an ask above this; the tail is model error")
    p.add_argument("--latency-ms", type=float, default=250.0)
    p.add_argument("--tick-ms", type=float, default=250.0)
    p.add_argument("--lock-mode", default="both", choices=["cost", "edge", "both"],
                   help="cost: pair under $1 (flattens winners). edge: hedge leg "
                        "must be +EV on its own. both: require each")
    p.add_argument("--bankroll", type=float, default=1000.0)
    p.add_argument("--kelly", type=float, default=0.25,
                   help="Kelly fraction for directional legs; 0 = flat max-usd")
    p.add_argument("--max-stale", type=float, default=20.0,
                   help="skip a book that has not changed in this many seconds")
    p.add_argument("--min-gap-bps", type=float, default=0.0,
                   help="minimum |spot - strike| in bps for the spot rule; "
                        "under 2bps the comparison is noise")
    p.add_argument("--spot-rule", type=int, default=0,
                   help="ignore the model's fair value and buy whichever side "
                        "spot sits on relative to the strike")
    p.add_argument("--slippage", type=float, default=0.0013,
                   help="adverse selection charged on every simulated fill, in "
                        "dollars per share; measured, not assumed")
    p.add_argument("--max-dd", type=float, default=0.25,
                   help="stop opening new legs once equity is this far below "
                        "its peak; hedging existing inventory continues")
    p.add_argument("--min-vrp", type=float, default=0.0,
                   help="require implied/realised sigma above this before "
                        "opening; the trade pays when the book prices more "
                        "uncertainty than the tape delivers. 0 disables")
    p.add_argument("--log-stale", type=int, default=0,
                   help="record stale quotes as samples instead of skipping "
                        "them.  Trading still respects --max-stale.")
    p.add_argument("--max-committed", type=float, default=0.0,
                   help="cap simultaneous open exposure at this fraction of "
                        "equity.  A drawdown limit stops opening but cannot "
                        "stop committed capital from settling against you; "
                        "this is what bounds the overshoot.  0 = uncapped.")
    p.add_argument("--maker-min-capture", type=float, default=-1.0,
                   help="only post when (mid - post price) is at least this. "
                        "With --maker-improve 1 that means only quoting where "
                        "the spread is wide enough to pay for the tick.")
    p.add_argument("--maker-improve", type=int, default=0,
                   help="post this many ticks above the best bid, buying queue "
                        "priority at the cost of the spread it was earning.")
    p.add_argument("--maker-cancel", type=float, default=0.04,
                   help="cancel a resting bid once the mid has moved this far "
                        "from it.  A bid that is merely no longer best is "
                        "still live and still fills.")
    p.add_argument("--maker", type=int, default=0,
                   help="post at the touch instead of taking.  Pays no fee and "
                        "earns the half-spread, at the cost of only filling "
                        "when someone chooses to hit you.")
    p.add_argument("--fav-only", type=int, default=0,
                   help="1: only buy the dearer side, -1: only the cheaper, "
                        "0: either.  Names a side by the complement's quote "
                        "rather than by a price threshold, which near the "
                        "money identifies both sides at once.")
    p.add_argument("--min-ask", type=float, default=0.0,
                   help="floor on the price paid; buying the favoured side is "
                        "the strongest predictor that a hedge will appear")
    p.add_argument("--max-depth", type=float, default=1e9,
                   help="ceiling on size resting at the best ask; a thin quote "
                        "is one about to move")
    p.add_argument("--max-intensity", type=float, default=1e9,
                   help="ceiling on relative traded notional; a busy tape means "
                        "makers have already widened")
    p.add_argument("--min-usd", type=float, default=1.0,
                   help="stop opening once free capital falls below this")
    p.add_argument("--adaptive-band", type=int, default=1,
                   help="scale the required locked profit by the fee payable and "
                        "how much of the outcome is still in doubt")
    p.add_argument("--er-min", type=float, default=0.0,
                   help="skip markets whose 30x1m efficiency ratio is below this "
                        "(0.0 = keep chop)")
    p.add_argument("--er-max", type=float, default=1.0,
                   help="skip markets above this efficiency ratio "
                        "(1.0 = keep one-way trends)")
    p.add_argument("--path-min", type=float, default=0.0,
                   help="minimum distance walked over 30x1m, in bps -- amplitude "
                        "filter; a dead tape offers nothing to either strategy")
    p.add_argument("--min-p-lock", type=float, default=0.0,
                   help="reflection-principle probability that a hedge becomes "
                        "available before the window closes; below this an "
                        "'open' leg is really a naked bet")
    p.add_argument("--shrink", type=float, default=1.0,
                   help="weight on the model vs the book's own price when "
                        "sizing; <1 is a confidence haircut on the model")
    p.add_argument("--min-intensity", type=float, default=0.0,
                   help="relative traded notional needed to open a leg; a lock "
                        "only appears if the price travels, and it travels "
                        "when the tape is busy. 0 disables the filter")
    p.add_argument("--vol-scale", type=int, default=1,
                   help="scale trailing sigma by sqrt(trade intensity)")
    p.add_argument("--no-coinbase", action="store_true")
    p.add_argument("--no-chainlink", action="store_true")
    p.add_argument("--db", default="paper3.db")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    a.window = 3600 if a.hourly else WIN
    if a.report:
        report(a)
    else:
        try:
            asyncio.run(main(a))
        except KeyboardInterrupt:
            print("\nbye")
