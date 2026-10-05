"""GRAD: Pump.fun graduation scanner (PAPER ONLY - fake money, no wallet, no keys).
Idea under test (2026-10-05): a token that fills its bonding curve and 'graduates' to the open market (pump-amm) has proven real
buying interest and its pool cannot be pulled, so buying right after graduation may have a better hit rate than buying at birth.
Flow: PumpPortal free websocket (subscribeMigration) -> mint -> DexScreener price (first reading once the pair is listed) -> paper buy
of EVERY graduate (no quality filters, so the raw hit rate is measured) -> baseline exit ladder -> a price path is stored for every
position so other exit rules can be replayed later.
Baseline ladder (constants below, from the pasted suggestion; NOT proven): stop -25% before 1.5x; sell 80% at 1.5x; the other 20% runs to
5x or a 12% trailing stop; 6h hard exit; rug rule = liquidity -60% AND price -50% in one step.
Costs: same as the other scanners (3% slippage in, 7% out, 1% fee each way, plus price impact = stake / liquidity)."""
import asyncio, json, os, threading, time, traceback, urllib.parse, urllib.request

API = "https://api.dexscreener.com"
WS_URL = "wss://pumpportal.fun/api/data"
UA = {"User-Agent": "Mozilla/5.0 (compatible; grad-paper-scanner; educational)", "Accept": "application/json"}
STATE = "grad_state.json"
BANK, STAKE, MAX_POS = 5000.0, 20.0, 100
SLIP_IN, SLIP_OUT, FEE, RUG_LIQ_DROP, RUG_PAYOUT = 0.03, 0.07, 0.01, 0.60, 0.30
STOP, T1, F_T1, T2, TRAIL, MAX_HOLD_H = 0.25, 1.5, 0.80, 5.0, 0.12, 6.0
PEND_MAX_S, TICK_S, PATH_EVERY_S, PATH_CAP = 600, 15, 30, 900
TOKEN, CHAT = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
LOCK, MIG = threading.Lock(), []


def tg(text):
    text = "[GRAD] " + text
    print(text)
    if not (TOKEN and CHAT):
        return
    try:
        d = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=d), timeout=20)
    except Exception as e:
        print("telegram failed:", e)


def f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def load():
    s = json.load(open(STATE)) if os.path.exists(STATE) else {}
    for k, v in (("cash", BANK), ("pend", {}), ("pos", []), ("closed", []), ("funnel", {}), ("seen", {}), ("last_hb", 0),
                 ("last_summary", ""), ("started", time.strftime("%F %T")), ("ws_events", 0)):
        s.setdefault(k, v)
    return s


def funnel(s, k, n=1):
    s["funnel"][k] = s["funnel"].get(k, 0) + n


def ws_thread():
    import websockets

    async def run():
        wait = 1
        while True:
            try:
                async with websockets.connect(WS_URL, open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    wait = 1
                    async for msg in ws:
                        try:
                            d = json.loads(msg)
                        except Exception:
                            continue
                        if d.get("txType") == "migrate" and d.get("mint"):
                            with LOCK:
                                MIG.append((time.time(), d))
            except Exception as e:
                print("ws error:", str(e)[:100], "- reconnect in", wait, "s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30)

    asyncio.run(run())


def dex_batch(mints):
    """mint -> best pair (highest liquidity) for up to 30 mints per call."""
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            req = urllib.request.Request(f"{API}/tokens/v1/solana/{','.join(chunk)}", headers=UA)
            pairs = json.load(urllib.request.urlopen(req, timeout=20))
        except Exception as e:
            print("dexscreener failed:", str(e)[:100])
            continue
        for p in pairs if isinstance(pairs, list) else []:
            m = (p.get("baseToken") or {}).get("address")
            if m in chunk and f(p.get("priceUsd")) > 0:
                if m not in out or f((p.get("liquidity") or {}).get("usd")) > f((out[m].get("liquidity") or {}).get("usd")):
                    out[m] = p
    return out


def sell(s, pos, tokens, price, rug=False):
    tokens = min(tokens, pos["tokens"])
    if tokens <= 0:
        return
    impact = STAKE / max(pos["liq_entry"], 1.0)
    got = tokens * price * (RUG_PAYOUT if rug else (1 - SLIP_OUT - impact) * (1 - FEE))
    got = max(got, 0.0)
    pos["tokens"] -= tokens
    pos["proceeds"] += got
    s["cash"] += got


def close(s, pos, why):
    sell(s, pos, pos["tokens"], pos.get("last", pos["entry_obs"]), rug=why.startswith("rug"))
    pos["reason"], pos["closed"] = why, time.strftime("%F %T")
    pos["mult_net"] = round(pos["proceeds"] / STAKE, 3)
    pos["pnl"] = round(pos["proceeds"] - STAKE, 2)
    s["pos"].remove(pos)
    s["closed"].append(pos)


def open_pos(s, mint, ev_t, p):
    price, liq = f(p["priceUsd"]), f((p.get("liquidity") or {}).get("usd"))
    if liq <= 0:
        return False                                          # liquidity not populated yet: keep waiting (up to PEND_MAX_S)
    if len(s["pos"]) >= MAX_POS or s["cash"] < STAKE:
        funnel(s, "skip_capacity"); return True
    fill = price * (1 + SLIP_IN + STAKE / max(liq, 1.0))
    s["cash"] -= STAKE
    now = time.time()
    s["pos"].append({"mint": mint, "sym": ((p.get("baseToken") or {}).get("symbol") or "?")[:12], "pair": p.get("pairAddress"),
                     "dex": p.get("dexId"), "entry_obs": price, "fill": fill, "liq_entry": liq,
                     "mcap_entry": f(p.get("marketCap") or p.get("fdv")), "tokens0": STAKE * (1 - FEE) / fill,
                     "tokens": STAKE * (1 - FEE) / fill, "proceeds": 0.0, "peak": price, "t1": False, "t_event": ev_t,
                     "delay_s": round(now - ev_t, 1), "t_open": now, "opened": time.strftime("%F %T"), "miss": 0,
                     "path": [[int(now), price, liq]], "last": price, "last_mult": 1.0, "peak_mult": 1.0})
    funnel(s, "opened")
    return True


def step(s):
    now = time.time()
    with LOCK:
        new, MIG[:] = list(MIG), []
    for t, d in new:
        s["ws_events"] += 1
        m = d["mint"]
        if m in s["seen"]:
            continue
        s["seen"][m] = int(t)
        s["pend"][m] = t
        funnel(s, "graduations_seen")
    s["seen"] = {k: v for k, v in s["seen"].items() if now - v < 86400}
    held = {p["mint"] for p in s["pos"]}
    mints = list(s["pend"]) + [p["mint"] for p in s["pos"]]
    if not mints:
        return
    data = dex_batch(mints)
    for m, t in list(s["pend"].items()):                      # waiting for DexScreener to list the new pair
        if m in data and m not in held:
            if open_pos(s, m, t, data[m]):
                del s["pend"][m]
            elif now - t > PEND_MAX_S:
                funnel(s, "never_had_liquidity"); del s["pend"][m]
        elif now - t > PEND_MAX_S:
            funnel(s, "never_listed"); del s["pend"][m]
    for pos in list(s["pos"]):
        p = data.get(pos["mint"])
        if not p:
            pos["miss"] += 1
            if pos["miss"] >= 10:                             # ~2.5 min of no price: treat as gone
                pos["last"] = 0.0; close(s, pos, "vanished")
            continue
        pos["miss"] = 0
        price, liq = f(p["priceUsd"]), f((p.get("liquidity") or {}).get("usd"))
        prev_p, prev_l = pos["last"], pos.get("liq_last", pos["liq_entry"])
        pos["last"], pos["liq_last"] = price, (liq if liq > 0 else prev_l)
        m = price / pos["entry_obs"]
        pos["last_mult"], pos["peak_mult"] = round(m, 3), round(max(pos["peak_mult"], m), 3)
        if now - pos["path"][-1][0] >= PATH_EVERY_S:
            pos["path"] = (pos["path"] + [[int(now), price, liq]])[-PATH_CAP:]
        if liq < prev_l * (1 - RUG_LIQ_DROP) and price <= prev_p * 0.5:
            sell(s, pos, pos["tokens"], price, rug=True); close(s, pos, "rug-liquidity-collapse"); continue
        pos["peak"] = max(pos["peak"], price)
        if not pos["t1"]:
            if m <= 1 - STOP:
                close(s, pos, "stop"); continue
            if m >= T1:
                pos["t1"] = True
                sell(s, pos, pos["tokens0"] * F_T1, price)
                pos["peak"] = price
        else:
            if m >= T2:
                close(s, pos, "5x"); continue
            if price <= pos["peak"] * (1 - TRAIL):
                close(s, pos, "trail"); continue
        if now - pos["t_open"] >= MAX_HOLD_H * 3600:
            close(s, pos, "time-6h")


def stats(s):
    allp = s["closed"] + s["pos"]
    n = len(allp)
    if not n:
        return "no entries yet"
    r = lambda x: sum(1 for p in allp if p["peak_mult"] >= x)
    c = [p["mult_net"] for p in s["closed"]]
    d = sorted(p["delay_s"] for p in allp)
    return (f"entries {n} (closed {len(c)}, open {len(s['pos'])}) | reached 1.25x {r(1.25)} ({r(1.25)*100//n}%) | 1.5x {r(1.5)} ({r(1.5)*100//n}%) "
            f"| 2x {r(2)} ({r(2)*100//n}%) | 5x {r(5)} ({r(5)*100//n}%)"
            + (f" | closed avg {sum(c)/len(c):.2f}x median {sorted(c)[len(c)//2]:.2f}x" if c else "")
            + f" | median entry delay after graduation {d[len(d)//2]:.0f}s")


def summary(s):
    eq = s["cash"] + sum(p["tokens"] * p["last"] * (1 - SLIP_OUT) * (1 - FEE) for p in s["pos"])
    return (f"Graduation paper scanner | equity ${eq:,.0f} (start ${BANK:,.0f}) | cash ${s['cash']:,.0f}\n" + stats(s)
            + "\nFunnel: " + ", ".join(f"{k} {v}" for k, v in sorted(s["funnel"].items())))


def main(s):
    step(s)
    if time.time() - s["last_hb"] > 3 * 3600:
        s["last_hb"] = time.time()
        tg(f"alive: graduations seen {s['funnel'].get('graduations_seen', 0)} | ws events {s['ws_events']} | open {len(s['pos'])} | closed {len(s['closed'])}")
    today = time.strftime("%F", time.gmtime())
    if time.gmtime().tm_hour >= 8 and s["last_summary"] != today:
        s["last_summary"] = today
        tg("Daily summary\n" + summary(s))
    s["cash"] = round(s["cash"], 4)
    if time.time() - s.get("_saved", 0) >= 60:
        s["_saved"] = time.time()
        json.dump(s, open(STATE, "w"))


if __name__ == "__main__":
    loop_min = float(os.environ.get("LOOP_MIN") or 0)
    end = time.time() + loop_min * 60
    threading.Thread(target=ws_thread, daemon=True).start()
    time.sleep(3)
    if not os.path.exists(STATE):
        tg("started: paper-buys every Pump.fun graduation (no filters) to measure the raw hit rate. No real money.")
    s = load()
    while True:
        t0 = time.time()
        try:
            main(s)
        except Exception:
            print("tick failed:", traceback.format_exc()[-600:])
        if time.time() + TICK_S >= end:
            break
        time.sleep(max(1, TICK_S - (time.time() - t0)))
    json.dump(s, open(STATE, "w"))
    print(summary(s))
