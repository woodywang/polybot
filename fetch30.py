"""Pull 30 days of 1-minute klines so the hourly calibration can be split by week.

The one-week backtest showed the model overstating Up below even money by up to
9 points, which matched that week's 47.5% base rate. Whether that is a model
defect or one falling week is only answerable across regimes.
"""
import json, urllib.request, time

UA = {"User-Agent": "Mozilla/5.0"}


def klines(sym, start_ms, limit=1000):
    u = (f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1m"
         f"&startTime={start_ms}&limit={limit}")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 * (attempt + 1))
    return []


out, now = {}, int(time.time())
for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
    bars, t, n = [], (now - 30 * 86400) * 1000, 0
    while t < now * 1000:
        b = klines(sym, t)
        if not b:
            break
        bars += b
        t = int(b[-1][0]) + 60000
        n += 1
        if n % 10 == 0:
            print(f"  {sym} {len(bars):>6} bars "
                  f"{time.strftime('%m-%d %H:%M', time.gmtime(int(b[-1][0])//1000))}", flush=True)
        if len(b) < 1000:
            break
    out[sym] = [(int(x[0]) // 1000, float(x[1]), float(x[4])) for x in bars]
    print(f"{sym}: {len(bars)} bars", flush=True)
json.dump(out, open("klines30.json", "w"))
print("done", flush=True)
