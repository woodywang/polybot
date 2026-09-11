import asyncio, json, time, urllib.request, collections, websockets, run
UA = run.UA
def get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20) as r:
        return json.load(r)

async def rate(toks, label, secs=30):
    n = collections.Counter(); t0 = time.time()
    async with websockets.connect(
            'wss://ws-subscriptions-clob.polymarket.com/ws/market',
            ping_interval=None, max_queue=None) as ws:
        await ws.send(json.dumps({'assets_ids': toks, 'type': 'market'}))
        while time.time() - t0 < secs:
            try:
                raw = await asyncio.wait_for(ws.recv(), 8)
            except asyncio.TimeoutError:
                continue
            if raw == 'PONG':
                continue
            j = json.loads(raw); msgs = j if isinstance(j, list) else [j]
            n['frames'] += 1; n['bytes'] += len(raw)
            for m in msgs:
                n[m.get('event_type')] += 1
                if m.get('event_type') == 'price_change':
                    n['levels'] += len(m.get('price_changes', []))
    el = time.time() - t0
    print(f'{label}: {len(toks)} tokens over {el:.0f}s')
    print(f'   frames/s {n["frames"]/el:8.1f}   KB/s {n["bytes"]/el/1024:7.1f}'
          f'   level-updates/s {n["levels"]/el:8.1f}', flush=True)

async def main():
    b5 = int(time.time() // 300) * 300
    t5 = []
    for a in ('btc', 'eth', 'sol'):
        for k in (0, 1, 2):
            m = get(f'https://gamma-api.polymarket.com/markets?slug={a}-updown-5m-{b5+k*300}&closed=false')
            if m: t5 += json.loads(m[0]['clobTokenIds'])
    bh = int(time.time() // 3600) * 3600
    th = []
    for a in ('btc', 'eth', 'sol'):
        for k in (0, 1):
            m = get(f'https://gamma-api.polymarket.com/markets?slug={run.hourly_slug(a,bh+k*3600)}&closed=false')
            if m: th += json.loads(m[0]['clobTokenIds'])
    await rate(t5, '5-minute')
    await rate(th, 'hourly  ')
asyncio.run(main())
