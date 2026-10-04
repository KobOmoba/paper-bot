"""V7 SNIPER (PAPER ONLY - no keys, no wallet, no orders, no Jito, no Jupiter).

This is the AariNAT V5 write-up + code, run on paper money, rule for rule:

  Loop 1  birth detection  : PumpPortal websocket (subscribeNewToken)           [spec: Helius gRPC InitializeMint]
  Loop 2  Fast Gate + buy  : mint authority null, freeze authority null, real SOL in bonding curve >= MIN_CURVE_SOL;
                             then a PAPER buy of TRADE_SIZE_SOL priced on the real Pump.fun curve
  Loop 3  state machine    : OPEN -> TIER_1_HIT -> TIER_2_HIT -> SHAVING -> CLOSED
      2x   : sell 60% of the initial tokens
      4x   : sell 30% of the initial tokens
      6x, 15x, 50x, 150x : each sells 50% of what is left (ATH resets to the current price at every shave)
      after the last shave : 15% trailing stop from ATH on the final bag
      no 2x within 12 h    : exit 100%
      liquidity collapse   : real SOL in the curve down >= 60% versus the previous scan -> emergency full exit
Multiples are measured on CAPITAL INVESTED (what the whole original position would pay out now, after curve impact,
fees and the Jito tip, divided by what was put in), as instructed.
After the curve completes (graduation) the price comes from DexScreener (priceNative, in SOL).
"""
import asyncio, base64, csv, json, os, struct, threading, time, urllib.request, statistics

STATE = os.environ.get("V7_STATE_FILE", "v7_state.json")
WS_URL = "wss://pumpportal.fun/api/data"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"

# ---- spec constants (from the write-up / code) ----
TRADE_SIZE_SOL = 0.05
JITO_TIP_SOL = 5000 / 1e9
TIER_1_TARGET, TIER_1_SELL_PCT = 2.0, 0.60
TIER_2_TARGET, TIER_2_SELL_PCT = 4.0, 0.30
SHAVE_TRIGGERS = [6.0, 15.0, 50.0, 150.0]
FINAL_TRAILING_STOP = 0.15
TIME_EXIT_SECONDS = 43200
RUG_LIQUIDITY_DROP_PCT = 0.60
# ---- the one gate value: 0.02 per the user's instruction (the write-up text says 2.0) ----
MIN_CURVE_SOL = float(os.environ.get("V7_MIN_CURVE_SOL", "0.02"))
# ---- paper execution model (replaces Jito/Jupiter; not in the spec) ----
FEE = 0.01                  # Pump.fun fee, each side
LAND_SLIP = 0.03            # price moves against us while a real order would land (buys only)
ENTRY_DELAY_S = 2.0         # first curve read after the birth event
AMM_SLIP, AMM_FEE = 0.05, 0.01   # exit model after graduation
BANK = 100000.0             # paper SOL: large on purpose so cash never blocks a buy
MAX_TRACKED = 30000
SUPPLY = 1e9                # Pump.fun tokens: 1,000,000,000 supply -> market cap = price x 1e9
SOL_USD = [float(os.environ.get('V7_SOL_USD', '0') or 0)]
CSV_FILE = os.environ.get('V7_CSV_FILE', 'v7_positions.csv')
# display labels only (not trading rules)
LBL_RUG_M, LBL_MOON_M, LBL_DEAD_S = 0.20, 2.0, 1200


def f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


# ---------------- Pump.fun curve maths ----------------
def curve_buy(vs, vt, sol):
    net = sol * (1 - FEE)
    toks = vt - vs * vt / (vs + net)
    return toks, sol / toks


def curve_sell(vs, vt, toks):
    return max((vs - vs * vt / (vt + toks)) * (1 - FEE) - JITO_TIP_SOL, 0.0)


def decode_curve(raw):
    if not raw or len(raw) < 49:
        return None
    vt, vs, rt, rs, sup = struct.unpack_from("<5Q", raw, 8)
    if vt <= 0 or vs <= 0:
        return None
    return {"vt": vt / 1e6, "vs": vs / 1e9, "real_sol": rs / 1e9, "complete": bool(raw[48])}


def decode_mint(raw):
    """SPL mint: [0:4] mint-authority option tag, [46:50] freeze-authority option tag. 0 = null (revoked)."""
    if not raw or len(raw) < 82:
        return None
    return {"mint_auth_null": struct.unpack_from("<I", raw, 0)[0] == 0, "freeze_auth_null": struct.unpack_from("<I", raw, 46)[0] == 0}


# ---------------- state machine (pure) ----------------
def act(pos, mid, liq, sell_fn, now):
    """One scan of one position. sell_fn(tokens)->SOL received. `liq` = liquidity reading (real SOL in curve /
    quote-side SOL after graduation). Returns events. Mirrors the spec's position_monitor."""
    ev = []
    if pos["status"] == "CLOSED":
        return ev

    def sell(tokens):
        tokens = min(tokens, pos["tokens"])
        pos["proceeds"] += sell_fn(tokens)
        pos["tokens"] -= tokens

    def close(why):
        pos["status"], pos["reason"], pos["tokens"] = "CLOSED", why, 0.0
        ev.append(("close", why))

    # RUG PROTECTION first, bypasses every tier
    prev = pos.get("prev_liq")
    pos["prev_liq"] = liq
    if prev is not None and prev > 0 and liq < prev * (1.0 - RUG_LIQUIDITY_DROP_PCT):
        sell(pos["tokens"]); close("rug-liquidity-collapse"); return ev

    m = sell_fn(pos["tokens0"]) / pos["cost"]            # capital multiple of the whole original position
    pos["m_now"] = m
    pos["peak_m"] = max(pos.get("peak_m", 0.0), m)
    if mid > pos["ath"]:
        pos["ath"] = mid

    progressed = True
    while progressed and pos["status"] != "CLOSED":      # a gap may clear several tiers; the spec would do them on consecutive scans
        progressed = False
        st = pos["status"]
        if st == "OPEN":
            if m >= TIER_1_TARGET:
                sell(pos["tokens0"] * TIER_1_SELL_PCT); pos["status"] = "TIER_1_HIT"; ev.append(("tier", 2)); progressed = True
            elif now - pos["entry_t"] >= TIME_EXIT_SECONDS:
                sell(pos["tokens"]); close("time-exit-12h")
        elif st == "TIER_1_HIT":
            if m >= TIER_2_TARGET:
                sell(pos["tokens0"] * TIER_2_SELL_PCT); pos["status"] = "TIER_2_HIT"; pos["shave_i"] = 0; ev.append(("tier", 4)); progressed = True
        elif st in ("TIER_2_HIT", "SHAVING"):
            i = pos.get("shave_i", 0)
            if i < len(SHAVE_TRIGGERS) and m >= SHAVE_TRIGGERS[i]:
                sell(pos["tokens"] * 0.5); pos["shave_i"] = i + 1; pos["status"] = "SHAVING"; pos["ath"] = mid
                ev.append(("shave", SHAVE_TRIGGERS[i])); progressed = True
            elif i >= len(SHAVE_TRIGGERS) and mid <= pos["ath"] * (1.0 - FINAL_TRAILING_STOP):
                sell(pos["tokens"]); close("trail-stop-final")
    return ev


# ---------------- engine ----------------
LOCK = threading.Lock()
BIRTHS = []
ST = {"tok": {}, "funnel": {}, "ws": 0, "agg": {"n": 0, "sum": 0.0, "wins": 0, "best": 0.0, "pnl": 0.0, "reasons": {}, "mults": [],
                                                  "reach": {}}, "closed": [], "cash": BANK}
RPC_BAD = {}


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
    good = [x for x in u if RPC_BAD.get(x, 0) < time.time()]
    return good or u


def rpc_multi(keys):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                       "params": [keys, {"encoding": "base64", "commitment": "confirmed"}]}).encode()
    for url in rpc_urls():
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "paper-v7"})
            r = json.load(urllib.request.urlopen(req, timeout=20))
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
    out = {}
    try:
        req = urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints), headers={"User-Agent": "paper-v7"})
        for p in json.load(urllib.request.urlopen(req, timeout=15)):
            a = (p.get("baseToken") or {}).get("address")
            liq = f((p.get("liquidity") or {}).get("usd"))
            if a and f(p.get("priceNative")) > 0 and (a not in out or liq > out[a][2]):
                out[a] = (f(p["priceNative"]), f((p.get("liquidity") or {}).get("quote")), liq)
    except Exception:
        funnel("dex_error")
    return out


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


def new_birth(t, d):
    ST["ws"] += 1
    if len(ST["tok"]) >= MAX_TRACKED:
        funnel("skip_capacity"); return
    ST["tok"][d["mint"]] = {"mint": d["mint"], "curve": d["bondingCurveKey"], "sym": (d.get("symbol") or "?")[:12], "born": t,
                            "due": t + ENTRY_DELAY_S, "tries": 0, "pos": None, "grad": False,
                            "feat": {"creator_buy_sol": f(d.get("solAmount")), "mcap0_sol": f(d.get("marketCapSol"))},
                            "launch_price": f(d.get("vSolInBondingCurve")) / max(f(d.get("vTokensInBondingCurve")), 1e-18)}


def interval(tk, now):
    """How often to re-read a position. Scan frequency only (not a rule): 5 s while young, slower later, slowest when dormant."""
    age = now - tk["born"]
    base = 5 if age < 600 else 30 if age < 3600 else 120
    if age > 3600 and tk.get("flat_reads", 0) >= 3:
        base = 600
    return base


def sell_curve(vs, vt):
    return lambda toks: curve_sell(vs, vt, toks)


def sell_amm(mid, liq_quote):
    def fn(toks):
        impact = (toks * mid) / max(2 * liq_quote, 1e-9)
        return max(toks * mid * (1 - AMM_SLIP - min(impact, 0.9)) * (1 - AMM_FEE) - JITO_TIP_SOL, 0.0)
    return fn


def open_position(tk, c, now):
    vs, vt = c["vs"], c["vt"]
    fvs, fvt = vs * (1 + LAND_SLIP), vt / (1 + LAND_SLIP)       # adverse move while the order lands
    toks, avg = curve_buy(fvs, fvt, TRADE_SIZE_SOL)
    cost = TRADE_SIZE_SOL + JITO_TIP_SOL
    ST["cash"] -= cost
    tk["pos"] = {"status": "OPEN", "tokens0": toks, "tokens": toks, "cost": cost, "proceeds": 0.0, "entry_mid": vs / vt, "ath": vs / vt,
                 "entry_t": now, "prev_liq": c["real_sol"], "fill_ratio": avg / (vs / vt)}
    p = tk["pos"]
    p.update({"entry_price": avg, "entry_mc_sol": (vs / vt) * SUPPLY, "curve_sol_entry": c["real_sol"], "last_price": vs / vt,
              "last_move": now, "sol_usd": SOL_USD[0]})
    tk["feat"]["curve_sol"] = round(c["real_sol"], 3)
    tk["due"] = now + interval(tk, now)
    funnel("entries")


def status_label(p, now):
    """RUGGED / MOONING / ACTIVE / DEAD from the last readings (display only)."""
    m = p.get("m_now", 1.0)
    if p.get("reason") == "rug-liquidity-collapse" or m <= LBL_RUG_M:
        return "RUGGED"
    if p.get("peak_m", 0.0) >= LBL_MOON_M:
        return "MOONING"
    if now - p.get("last_move", now) >= LBL_DEAD_S:
        return "DEAD"
    return "ACTIVE"


def iso(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) if t else ""


def row(tk, now):
    p, su = tk["pos"], (tk["pos"].get("sol_usd") or SOL_USD[0] or 0.0)
    return {"address": tk["mint"], "symbol": tk["sym"], "status": status_label(p, now),
            "launch_price_sol": f"{tk.get('launch_price', 0):.4e}", "entry_price_sol": f"{p.get('entry_price', 0):.4e}",
            "launch_mc_sol": round(tk["feat"].get("mcap0_sol", 0), 2), "entry_mc_sol": round(p.get("entry_mc_sol", 0), 2),
            "entry_mc_usd": round(p.get("entry_mc_sol", 0) * su),
            "curve_sol_entry": round(p.get("curve_sol_entry", 0), 3), "liquidity_usd_entry": round(p.get("curve_sol_entry", 0) * su, 2),
            "born_utc": iso(tk["born"]), "entry_utc": iso(p["entry_t"]), "age_min": round((now - tk["born"]) / 60, 1),
            "last_price_sol": f"{p.get('last_price', 0):.4e}", "curve_sol_now": round(p.get("prev_liq") or 0, 3),
            "current_x": round(p.get("m_now", 0), 2), "peak_x": round(p.get("peak_m", 0), 2), "tier_state": p["status"],
            "closed_reason": p.get("reason", ""), "stake_sol": TRADE_SIZE_SOL, "proceeds_sol": round(p.get("proceeds", 0), 5),
            "result_x": round(p["proceeds"] / p["cost"], 3) if p["status"] == "CLOSED" else ""}


def record(tk, now):
    p = tk["pos"]
    mu = p["proceeds"] / p["cost"]
    ST["cash"] += p["proceeds"]
    a = ST["agg"]
    a["n"] += 1; a["sum"] += mu; a["wins"] += mu > 1; a["best"] = max(a["best"], mu); a["pnl"] += p["proceeds"] - p["cost"]
    a["reasons"][p["reason"]] = a["reasons"].get(p["reason"], 0) + 1
    a["mults"].append(round(mu, 3)); del a["mults"][:-30000]
    pk = p.get("peak_m", 0.0)
    for k in (2, 4, 6, 15, 50, 150):
        if pk >= k:
            a["reach"][str(k)] = a["reach"].get(str(k), 0) + 1
    ST["closed"].append({"sym": tk["sym"], "mint": tk["mint"], "mult": round(mu, 3), "reason": p["reason"], "peak_m": round(pk, 2),
                         "held_min": round((now - p["entry_t"]) / 60, 1), "feat": tk["feat"], "row": row(tk, now)})
    del ST["closed"][:-6000]
    ST["tok"].pop(tk["mint"], None)


def drive(tk, mid, liq, sell_fn, now):
    p = tk["pos"]
    last = tk.get("last_mid")
    tk["flat_reads"] = tk.get("flat_reads", 0) + 1 if (last and abs(mid / last - 1) < 0.001) else 0
    if not (last and abs(mid / last - 1) < 0.001):
        p["last_move"] = now
    p["last_price"] = mid
    tk["last_mid"] = mid
    for kind, val in act(p, mid, liq, sell_fn, now):
        if kind == "shave" or (kind == "tier" and val >= 4):
            tg(f"{'SHAVE' if kind == 'shave' else 'TIER'} {val}x {tk['sym']} ({tk['mint'][:6]}..) | now {p['m_now']:.1f}x capital")
        if kind == "close":
            record(tk, now)
    if p["status"] != "CLOSED":
        tk["due"] = now + (20 if tk["grad"] else interval(tk, now))


def tick(now, fetch=rpc_multi, dexfn=dex_prices):
    with LOCK:
        b, BIRTHS[:] = list(BIRTHS), []
    for t, d in b:
        new_birth(t, d)
    toks = list(ST["tok"].values())
    # Loop 2: Fast Gate for tokens with no position yet (curve + mint account in one call)
    pre = [tk for tk in toks if tk["pos"] is None and now >= tk["due"]]
    for i in range(0, len(pre), 50):
        chunk = pre[i:i + 50]
        raws = fetch([k for tk in chunk for k in (tk["curve"], tk["mint"])])
        if raws is None:
            continue
        for j, tk in enumerate(chunk):
            c, mi = decode_curve(raws[2 * j]), decode_mint(raws[2 * j + 1])
            if c is None or mi is None:
                tk["tries"] += 1; tk["due"] = now + 2
                if tk["tries"] >= 4:
                    funnel("skip_no_account"); ST["tok"].pop(tk["mint"], None)
                continue
            if c["complete"]:
                funnel("skip_born_complete"); ST["tok"].pop(tk["mint"], None); continue
            if not (mi["mint_auth_null"] and mi["freeze_auth_null"]):
                funnel("fail_authority"); ST["tok"].pop(tk["mint"], None); continue
            if c["real_sol"] < MIN_CURVE_SOL:
                funnel("fail_curve_sol"); ST["tok"].pop(tk["mint"], None); continue
            open_position(tk, c, now)
    # Loop 3: positions still on the curve
    live = [tk for tk in toks if tk["pos"] is not None and not tk["grad"] and tk["mint"] in ST["tok"] and now >= tk["due"]]
    for i in range(0, len(live), 100):
        chunk = live[i:i + 100]
        raws = fetch([tk["curve"] for tk in chunk])
        if raws is None:
            continue
        for tk, raw in zip(chunk, raws):
            c = decode_curve(raw)
            if c is None:
                tk["due"] = now + 10; continue
            if c["complete"]:
                tk["grad"], tk["due"] = True, now
                funnel("graduated"); continue
            drive(tk, c["vs"] / c["vt"], c["real_sol"], sell_curve(c["vs"], c["vt"]), now)
    # graduated positions: DexScreener
    grads = [tk for tk in toks if tk["pos"] is not None and tk["grad"] and tk["mint"] in ST["tok"] and now >= tk["due"]]
    for i in range(0, len(grads), 30):
        chunk = grads[i:i + 30]
        px = dexfn([tk["mint"] for tk in chunk])
        for tk in chunk:
            r = px.get(tk["mint"])
            tk["due"] = now + 20
            if not r:
                tk["dex_miss"] = tk.get("dex_miss", 0) + 1
                continue
            tk["dex_miss"] = 0
            drive(tk, r[0], r[1], sell_amm(r[0], r[1]), now)


# ---------------- persistence / reporting ----------------
def write_csv(now=None):
    now = now or time.time()
    rows = [c["row"] for c in ST["closed"] if c.get("row")] + [row(t, now) for t in ST["tok"].values() if t["pos"]]
    if not rows:
        return
    with open(CSV_FILE + ".tmp", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    os.replace(CSV_FILE + ".tmp", CSV_FILE)


def save():
    write_csv()
    d = {"ST": {k: ST[k] for k in ("funnel", "ws", "agg", "closed", "cash")}, "tok": ST["tok"], "saved": time.strftime("%F %T"), "schema": "v7-spec-1"}
    json.dump(d, open(STATE + ".tmp", "w"), separators=(",", ":"))
    os.replace(STATE + ".tmp", STATE)


def load():
    try:
        d = json.load(open(STATE))
    except Exception:
        return
    if d.get("schema") != "v7-spec-1":
        return
    ST.update(d["ST"])
    ST["tok"] = d["tok"]
    for tk in ST["tok"].values():
        tk["due"] = 0


def summary():
    a, tk = ST["agg"], ST["tok"]
    op = [t for t in tk.values() if t["pos"]]
    L = [f"V7 SNIPER paper (spec rules) | births seen {ST['ws']} | open positions {len(op)} | waiting for first read {len(tk) - len(op)}"]
    stc = {}
    for t in op:
        stc[t["pos"]["status"]] = stc.get(t["pos"]["status"], 0) + 1
    now = time.time()
    lab = {}
    for t in op:
        k = status_label(t["pos"], now); lab[k] = lab.get(k, 0) + 1
    if lab:
        L.append("market status of open positions: " + ", ".join(f"{k} {lab.get(k, 0)}" for k in ("MOONING", "ACTIVE", "DEAD", "RUGGED")))
        top = sorted(op, key=lambda t: -t["pos"].get("m_now", 0))[:5]
        L.append("top 5 now: " + " | ".join(f"{t['sym']} {t['pos'].get('m_now', 0):.1f}x" for t in top))
    if stc:
        L.append("open by state: " + ", ".join(f"{k} {v}" for k, v in sorted(stc.items())))
    if a["n"]:
        L.append(f"closed {a['n']} | avg {a['sum'] / a['n']:.2f}x | median {statistics.median(a['mults']):.2f}x | win {100 * a['wins'] / a['n']:.1f}% "
                 f"| best {a['best']:.1f}x | P&L {a['pnl']:+.2f} SOL (stake {TRADE_SIZE_SOL} SOL)")
        L.append("exits: " + ", ".join(f"{k} {v}" for k, v in sorted(a["reasons"].items(), key=lambda kv: -kv[1])))
        L.append("tokens that reached (capital basis): " + " | ".join(f"{k}x {a['reach'].get(k, 0)}" for k in ("2", "4", "6", "15", "50", "150")))
    else:
        L.append("no closed positions yet (a position closes at the 12 h time-exit, a rug, or the final trailing stop)")
    L.append("funnel: " + ", ".join(f"{k} {v}" for k, v in sorted(ST["funnel"].items())))
    return "\n".join(L)


def main():
    loop_min = float(os.environ.get("LOOP_MIN", "0"))
    load()
    fetch_sol_usd()
    threading.Thread(target=ws_thread, daemon=True).start()
    t0 = last_save = last_sum = last_alive = time.time()
    tg(f"V7 sniper job started (paper only) | {TRADE_SIZE_SOL} SOL per trade | gate: authorities null + curve >= {MIN_CURVE_SOL} SOL | "
       f"ladder 2x/4x + shaves {SHAVE_TRIGGERS} + {int(FINAL_TRAILING_STOP * 100)}% trail | open {sum(1 for t in ST['tok'].values() if t['pos'])}")
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
            tg(f"alive: births {ST['ws']}"); last_alive = now
        if loop_min and now - t0 > loop_min * 60:
            break
        time.sleep(1.0)
    save()
    tg(summary())


if __name__ == "__main__":
    main()
