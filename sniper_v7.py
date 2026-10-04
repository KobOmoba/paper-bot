"""V7 SNIPER (PAPER ONLY - no keys, no wallet, no orders).

One idea: catch every Pump.fun launch the moment it is born (websocket, no DexScreener), paper-buy it,
and sell only with the Ascending Shave (half of what is left at 10x/25x/100x/300x on capital invested,
shaving only once the bag is worth >= $1000; below that, once capital has doubled, a trailing exit).
  PumpPortal websocket (subscribeNewToken) -> after ENTRY_DELAY_S read the bonding-curve account -> paper fill priced
  with the real curve (impact + 1% fee + landing cushion) -> re-read curves in batches -> state machine per position.
After the curve completes (token graduates) price comes from DexScreener (priceNative, in SOL).
"""
import asyncio, base64, json, os, struct, sys, threading, time, urllib.request, statistics

STATE = os.environ.get("V7_STATE_FILE", "v7_state.json")
WS_URL = "wss://pumpportal.fun/api/data"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
STAKE = float(os.environ.get("V7_STAKE_SOL", "0.25"))     # SOL per strategy per token
BANK = 5000.0                                              # paper SOL per strategy (large on purpose: never starve a strategy of cash)
FEE = 0.01                                                 # pump.fun fee, both sides
LAND_SLIP = 0.03                                           # extra adverse move while a real order would land
ENTRY_DELAY_S = 2.0
TRACK_MIN = 90                                             # follow a token this long, then force-close
MAX_TRACKED = 800
SHAVE_MIN_USD = float(os.environ.get("V7_SHAVE_MIN_USD", "1000"))   # shaving only once the bag is worth this much
SOL_USD = [float(os.environ.get("V7_SOL_USD", "0") or 0)]          # filled from DexScreener at start if not given
AMM_SLIP, AMM_FEE = 0.05, 0.01                             # exit model after graduation
STAGNANT_MIN = 20                                          # price frozen this long -> stop polling, close

STRATS = {   # ONE strategy: buy at birth, hold, sell only by the Ascending Shave. No stop-loss, no deadline.
    "SHAVE": dict(stop=None, deadline_min=None, shaves=[(10, .5), (25, .5), (100, .5), (300, .5)], trail_mid=0.30, trail_final=0.15, gate=True),
}


def f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


# ---------------- curve maths (pure) ----------------
def curve_buy(vs, vt, sol):
    """spend `sol` SOL on a curve with virtual reserves (vs SOL, vt tokens) -> (tokens, avg_price)"""
    net = sol * (1 - FEE)
    toks = vt - vs * vt / (vs + net)
    return toks, sol / toks


def curve_sell(vs, vt, toks):
    """sell `toks` tokens into the curve -> SOL received after fee"""
    return (vs - vs * vt / (vt + toks)) * (1 - FEE)


def decode_curve(raw):
    if len(raw) < 49:
        return None
    vt, vs, rt, rs, sup = struct.unpack_from("<5Q", raw, 8)
    if vt <= 0 or vs <= 0:
        return None
    return {"vt": vt / 1e6, "vs": vs / 1e9, "complete": bool(raw[48])}


# ---------------- state machine (pure) ----------------
class Leg:
    """One strategy's position in one token."""

    def __init__(self, name, tokens, cost, mid, t):
        self.name, self.tokens, self.tokens0, self.cost, self.entry_mid, self.t0 = name, tokens, tokens, cost, mid, t
        self.proceeds, self.stage, self.peak, self.closed, self.reason = 0.0, 0, mid, False, None

    def to_d(self):
        return dict(self.__dict__)

    @staticmethod
    def from_d(d):
        l = Leg(d["name"], d["tokens"], d["cost"], d["entry_mid"], d["t0"])
        l.__dict__.update(d)
        return l


def step(leg, cfg, mid, sell_fn, now, min_sol=0.0):
    """Feed one price (`mid`) to a leg. sell_fn(tokens)->SOL actually received.
    Multiples (2x, 4x, stop %) are measured on CAPITAL INVESTED: m = what the WHOLE original position would pay out
    right now, after impact and fees, divided by what we put in. So '2x' means a 2x payout net of costs.
    Shaving (cfg gate) only happens once the remaining bag is worth >= min_sol; before that, once m >= 2 the position
    is simply trailed (all-or-nothing) instead of shaved."""
    ev = []
    if leg.closed:
        return ev
    m = sell_fn(leg.tokens0) / leg.cost
    leg.m_now = m
    leg.peak = max(leg.peak, mid)
    gated = cfg.get("gate")

    def dump(why):
        leg.proceeds += sell_fn(leg.tokens)
        leg.tokens, leg.closed, leg.reason = 0.0, True, why
        ev.append(("close", why))

    while leg.stage < len(cfg["shaves"]) and m >= cfg["shaves"][leg.stage][0] and leg.tokens > 0:
        if gated and sell_fn(leg.tokens) < min_sol:
            break                                           # bag not big enough to be worth shaving yet
        mult, frac = cfg["shaves"][leg.stage]
        part = leg.tokens * min(frac, 1.0)
        leg.proceeds += sell_fn(part)
        leg.tokens -= part
        leg.stage += 1
        leg.peak = mid                                     # peak resets after every rung
        ev.append(("shave", mult))
        if leg.tokens <= leg.tokens0 * 1e-9:
            leg.tokens, leg.closed, leg.reason = 0.0, True, "all-sold"
            return ev
    if gated and leg.stage == 0 and not getattr(leg, "armed", False) and m >= 2:
        leg.armed, leg.peak = True, mid                     # capital doubled: start trailing instead of waiting for a shave
    live = leg.stage > 0 or getattr(leg, "armed", False)
    if not live:
        if cfg["stop"] is not None and m <= 1 + cfg["stop"]:
            dump("stop"); return ev
        if cfg["deadline_min"] and (now - leg.t0) / 60 >= cfg["deadline_min"]:
            dump("deadline"); return ev
    else:
        trail = cfg["trail_final"] if leg.stage >= len(cfg["shaves"]) else cfg["trail_mid"]
        if trail and mid <= leg.peak * (1 - trail):
            dump("trail"); return ev
    return ev


# ---------------- engine ----------------
LOCK = threading.Lock()
BIRTHS = []                      # raw events from the websocket thread
ST = {"strat": {}, "tok": {}, "done": [], "funnel": {}, "ws": 0, "started": time.time()}
RPC_BAD = {}                     # url -> time until which we skip it


def funnel(k, n=1):
    ST["funnel"][k] = ST["funnel"].get(k, 0) + n


def tg(text):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print(text)
    if not (tok and chat):
        return
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                     data=json.dumps({"chat_id": chat, "text": "[V7] " + text[:3900]}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("telegram failed:", str(e)[:80])


def rpc_urls():
    u = [x for x in [os.environ.get("SOLANA_RPC"), PUBLIC_RPC] if x]
    now = time.time()
    good = [x for x in u if RPC_BAD.get(x, 0) < now]
    return good or u


def rpc_multi(keys):
    """getMultipleAccounts for <=100 keys -> list of raw bytes|None"""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                       "params": [keys, {"encoding": "base64", "commitment": "confirmed"}]}).encode()
    for url in rpc_urls():
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "paper-v7"})
            r = json.load(urllib.request.urlopen(req, timeout=15))
            v = (r.get("result") or {}).get("value")
            if isinstance(v, list):
                return [base64.b64decode(a["data"][0]) if a else None for a in v]
            funnel("rpc_odd")
        except urllib.error.HTTPError as e:
            RPC_BAD[url] = time.time() + (600 if e.code in (401, 403) else 20)
            funnel(f"rpc_http_{e.code}")
        except Exception:
            funnel("rpc_error")
    return None


def dex_prices(mints):
    """DexScreener batch -> {mint: (priceNative, quoteLiquiditySOL)} for graduated tokens"""
    out = {}
    try:
        req = urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints),
                                     headers={"User-Agent": "paper-v7"})
        for p in json.load(urllib.request.urlopen(req, timeout=15)):
            a = (p.get("baseToken") or {}).get("address")
            liq = f((p.get("liquidity") or {}).get("usd"))
            if a and f(p.get("priceNative")) > 0 and (a not in out or liq > out[a][2]):
                out[a] = (f(p["priceNative"]), f((p.get("liquidity") or {}).get("quote")), liq)
    except Exception:
        funnel("dex_error")
    return out


def ws_thread():
    import websockets

    async def run():
        wait = 1
        while True:
            try:
                async with websockets.connect(WS_URL, open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    wait = 1
                    async for msg in ws:
                        try:
                            d = json.loads(msg)
                        except Exception:
                            continue
                        if d.get("txType") == "create" and d.get("mint") and d.get("bondingCurveKey"):
                            with LOCK:
                                BIRTHS.append((time.time(), d))
            except Exception as e:
                print("ws error:", str(e)[:100], "- reconnect in", wait, "s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30)

    asyncio.run(run())


def strat_state(n):
    return ST["strat"].setdefault(n, {"cash": BANK, "closed": [], "n": 0})


def new_birth(t, d):
    ST["ws"] += 1
    if len(ST["tok"]) >= MAX_TRACKED:
        funnel("skip_capacity"); return
    ST["tok"][d["mint"]] = {"mint": d["mint"], "curve": d["bondingCurveKey"], "sym": (d.get("symbol") or "?")[:12], "born": t,
                            "due": t + ENTRY_DELAY_S, "tries": 0, "legs": {}, "grad": False, "last_mid": None, "last_move": t,
                            "peak_mult": 1.0, "entry_mid": None,
                            "feat": {"creator_buy_sol": f(d.get("solAmount")), "mcap0_sol": f(d.get("marketCapSol")),
                                     "ev_price": f(d.get("vSolInBondingCurve")) / max(f(d.get("vTokensInBondingCurve")), 1e-9)}}


def interval(age_s):
    return 4 if age_s < 600 else 15 if age_s < 1800 else 45


def open_legs(tk, vs, vt, now):
    cost_mid = vs / vt
    fill_vs, fill_vt = vs * (1 + LAND_SLIP), vt / (1 + LAND_SLIP)   # price moved against us while the order lands
    toks, avg = curve_buy(fill_vs, fill_vt, STAKE)
    tk["entry_mid"] = cost_mid
    tk["fill_ratio"] = avg / cost_mid
    for n in STRATS:
        s = strat_state(n)
        if s["cash"] < STAKE:
            funnel("skip_cash_" + n); continue
        s["cash"] -= STAKE
        s["n"] += 1
        tk["legs"][n] = Leg(n, toks, STAKE, cost_mid, now)
    funnel("entries")


def MIN_SOL():
    return SHAVE_MIN_USD / SOL_USD[0] if SOL_USD[0] > 0 else 1e9      # unknown SOL price -> never shave (safe)


def fetch_sol_usd():
    if SOL_USD[0] > 0:
        return
    try:
        req = urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112",
                                     headers={"User-Agent": "paper-v7"})
        best = max(json.load(urllib.request.urlopen(req, timeout=15)), key=lambda p: f((p.get("liquidity") or {}).get("usd")))
        SOL_USD[0] = f(best.get("priceUsd"))
    except Exception as e:
        print("SOL price fetch failed:", str(e)[:80])


def sell_curve(vs, vt):
    return lambda toks: curve_sell(vs, vt, toks)


def sell_amm(mid, liq_quote):
    def fn(toks):
        impact = (toks * mid) / max(2 * liq_quote, 1e-9)
        return toks * mid * (1 - AMM_SLIP - min(impact, 0.9)) * (1 - AMM_FEE)
    return fn


def finalize_token(tk, why, now=None):
    for n, l in tk["legs"].items():
        if not l.closed and l.tokens > 0:
            # force-close at last known price with the same sell model
            last = tk["last_mid"] or l.entry_mid
            l.proceeds += l.tokens * last * (1 - AMM_SLIP) * (1 - FEE)
            l.tokens, l.closed, l.reason = 0.0, True, why
        if l.closed:
            record(tk, l, now)
    ST["done"].append({"mint": tk["mint"], "sym": tk["sym"], "born": int(tk["born"]), "peak_mult": round(tk["peak_mult"], 2),
                       "last_mult": round((tk["last_mid"] or 0) / tk["entry_mid"], 3) if tk["entry_mid"] and tk["last_mid"] else None,
                       "grad": tk["grad"], "feat": tk["feat"], "fill_ratio": round(tk.get("fill_ratio", 0), 3), "why": why})
    del ST["done"][:-4000]
    ST["tok"].pop(tk["mint"], None)


def record(tk, l, now=None):
    s = strat_state(l.name)
    if getattr(l, "_rec", False) or l.__dict__.get("rec"):
        return
    l.rec = True
    s["cash"] += l.proceeds
    mu = l.proceeds / l.cost
    a = s.setdefault("agg", {"n": 0, "sum": 0.0, "wins": 0, "best": 0.0, "pnl": 0.0, "reasons": {}, "mults": []})
    a["n"] += 1; a["sum"] += mu; a["wins"] += mu > 1; a["best"] = max(a["best"], mu); a["pnl"] += (mu - 1) * l.cost
    a["reasons"][l.reason] = a["reasons"].get(l.reason, 0) + 1
    a["mults"].append(round(mu, 3)); del a["mults"][:-20000]
    s["closed"].append({"sym": tk["sym"], "mint": tk["mint"], "mult": round(l.proceeds / l.cost, 3), "reason": l.reason,
                        "peak": round(l.peak / l.entry_mid, 2), "stage": l.stage, "held_min": round(((now or time.time()) - l.t0) / 60, 1)})
    del s["closed"][:-4000]


def tick(now, fetch=rpc_multi, dexfn=dex_prices):
    # 1. births -> tracked tokens
    with LOCK:
        b, BIRTHS[:] = list(BIRTHS), []
    for t, d in b:
        new_birth(t, d)
    # 2. which tokens are due for a read
    due = [tk for tk in ST["tok"].values() if now >= tk["due"] and not tk["grad"]]
    for i in range(0, len(due), 100):
        chunk = due[i:i + 100]
        raws = fetch([tk["curve"] for tk in chunk])
        if raws is None:
            continue
        for tk, raw in zip(chunk, raws):
            c = decode_curve(raw) if raw else None
            if c is None:
                tk["tries"] += 1
                tk["due"] = now + 2
                if not tk["legs"] and tk["tries"] >= 4:
                    funnel("skip_no_account"); ST["tok"].pop(tk["mint"], None)
                continue
            on_curve_read(tk, c, now)
    # 3. graduated tokens via DexScreener
    grads = [tk for tk in ST["tok"].values() if tk["grad"] and now >= tk["due"]]
    for i in range(0, len(grads), 30):
        chunk = grads[i:i + 30]
        px = dexfn([tk["mint"] for tk in chunk])
        for tk in chunk:
            tk["due"] = now + 20
            r = px.get(tk["mint"])
            if not r:
                tk["dex_miss"] = tk.get("dex_miss", 0) + 1
                if tk["dex_miss"] >= 6:
                    finalize_token(tk, "graduated-no-price", now)
                continue
            tk["dex_miss"] = 0
            drive(tk, r[0], sell_amm(r[0], r[1]), now)
    # 4. timeouts
    for tk in list(ST["tok"].values()):
        if tk["mint"] not in ST["tok"]:
            continue
        age = now - tk["born"]
        if tk["legs"] and all(l.closed for l in tk["legs"].values()) and age > 1800:
            finalize_token(tk, "legs-closed", now)
        elif age > TRACK_MIN * 60:
            finalize_token(tk, "track-end", now)
        elif tk["legs"] and now - tk["last_move"] > STAGNANT_MIN * 60:
            finalize_token(tk, "stagnant", now)


def on_curve_read(tk, c, now):
    vs, vt = c["vs"], c["vt"]
    mid = vs / vt
    if not tk["legs"] and tk["entry_mid"] is None:
        if c["complete"]:
            funnel("skip_born_complete"); ST["tok"].pop(tk["mint"], None); return
        open_legs(tk, vs, vt, now)
    if c["complete"]:
        tk["grad"], tk["due"] = True, now
        funnel("graduated")
        return
    drive(tk, mid, sell_curve(vs, vt), now)
    tk["due"] = now + interval(now - tk["born"])


def drive(tk, mid, sell_fn, now):
    if tk["last_mid"] is None or abs(mid / tk["last_mid"] - 1) > 0.001:
        tk["last_move"] = now
    tk["last_mid"] = mid
    if tk["entry_mid"]:
        tk["peak_mult"] = max(tk["peak_mult"], mid / tk["entry_mid"])      # token peak, raw price basis
    for n, l in tk["legs"].items():
        if l.closed:
            continue
        for kind, val in step(l, STRATS[n], mid, sell_fn, now, MIN_SOL()):
            if kind == "shave" and val >= 10 and n == "SHAVE":
                tg(f"SHAVE {val}x {tk['sym']} ({tk['mint'][:6]}..) | now {l.m_now:.0f}x capital")
            if kind == "close":
                record(tk, l, now)


# ---------------- persistence / reporting ----------------
def save():
    d = {"strat": ST["strat"], "funnel": ST["funnel"], "ws": ST["ws"], "done": ST["done"][-4000:], "saved": time.strftime("%F %T"), "bank_v": 2,
         "tok": {m: {**{k: v for k, v in tk.items() if k != "legs"}, "legs": {n: l.to_d() for n, l in tk["legs"].items()}}
                 for m, tk in ST["tok"].items()}}
    json.dump(d, open(STATE + ".tmp", "w"))
    os.replace(STATE + ".tmp", STATE)


def load():
    try:
        d = json.load(open(STATE))
    except Exception:
        return
    ST["strat"], ST["funnel"], ST["ws"], ST["done"] = d.get("strat", {}), d.get("funnel", {}), d.get("ws", 0), d.get("done", [])
    if d.get("bank_v") != 2:                                  # one-time paper top-up when the bank was raised from 300 to 5000
        for n in ST["strat"]:
            ST["strat"][n]["cash"] += 4700.0
    for m, tk in d.get("tok", {}).items():
        tk["legs"] = {n: Leg.from_d(l) for n, l in tk["legs"].items() if n in STRATS}   # drop legs of removed strategies
        tk["due"] = 0
        ST["tok"][m] = tk


def summary():
    L = [f"V7 SNIPER paper | births seen {ST['ws']} | tracking {len(ST['tok'])} | tokens in peak sample {len(ST['done'])}"]
    for n in STRATS:
        s = strat_state(n)
        a = s.get("agg")
        if not a:                                             # old state without running totals: seed from the capped list
            c = s["closed"]
            a = {"n": len(c), "sum": sum(x["mult"] for x in c), "wins": sum(x["mult"] > 1 for x in c), "best": max([x["mult"] for x in c] or [0]),
                 "pnl": sum((x["mult"] - 1) * STAKE for x in c), "reasons": {}, "mults": [x["mult"] for x in c]}
            s["agg"] = a
        if not a["n"]:
            L.append(f"{n}: no closed trades"); continue
        md = statistics.median(a["mults"])
        rs = ", ".join(f"{k} {v}" for k, v in sorted(a["reasons"].items(), key=lambda kv: -kv[1]))
        L.append(f"{n}: {a['n']} closed | avg {a['sum'] / a['n']:.2f}x | median {md:.2f}x | win {100 * a['wins'] / a['n']:.1f}% "
                 f"| best {a['best']:.1f}x | P&L {a['pnl']:+.1f} SOL | exits: {rs}")
    dn = ST["done"]
    if dn:
        pk = [x["peak_mult"] for x in dn]
        L.append("Peak reach (all tracked tokens, n=%d): " % len(pk) + " | ".join(f">={k}x {100 * sum(p >= k for p in pk) / len(pk):.1f}%" for k in (1.5, 2, 5, 10, 25, 100)))
    L.append("funnel: " + ", ".join(f"{k} {v}" for k, v in sorted(ST["funnel"].items())))
    return "\n".join(L)


def main():
    loop_min = float(os.environ.get("LOOP_MIN", "0"))
    load()
    fetch_sol_usd()
    threading.Thread(target=ws_thread, daemon=True).start()
    t0 = last_save = last_sum = last_alive = time.time()
    tg(f"V7 sniper job started (paper only) | stake {STAKE} SOL | SOL ${SOL_USD[0]:.0f} | shave only when bag >= ${SHAVE_MIN_USD:.0f} | strategies {', '.join(STRATS)}")
    while True:
        now = time.time()
        try:
            tick(now)
        except Exception as e:
            funnel("tick_error"); print("tick error:", repr(e)[:200])
        if now - last_save > 120:
            save(); last_save = now
        if now - last_sum > 3600:
            tg(summary()); last_sum = now
        if now - last_alive > 3 * 3600:
            tg(f"alive: births {ST['ws']}, tracking {len(ST['tok'])}"); last_alive = now
        if loop_min and now - t0 > loop_min * 60:
            break
        time.sleep(1.0)
    save()
    tg(summary())


if __name__ == "__main__":
    main()
