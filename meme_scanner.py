"""Meme-token scanner + PAPER simulator. FAKE money only. No wallet, no keys, no orders, ever.
Data: DexScreener public API (free, no key). Runs every ~15 minutes on GitHub Actions.
RULES = user's "V5 fast-momentum" rules (set 2026-10-03). Entry, ALL must pass:
  age 1-4h | liquidity $8k-$40k | market cap < $300k | 1h volume >= 3x the prior hourly average | buys >= 1.5x sells (1h).
Exits: hard stop -25% before Tier 1 | Tier 1 sell 50% at 2x | Tier 2 sell 30% at 4x (peak reset there) |
  moon bag 20% on a 12% trailing stop from post-4x peak | if 2x not reached within 12h, exit 100% |
  rug: liquidity falls >=60% within one scan step -> book a 70% loss (30% payout).
Pessimistic costs: entry slippage 3% + impact, exit slippage 7% + impact, 1% fee per side. Exits execute at the price
observed AFTER a breach (15-minute gaps), never at the stop level.
MY ASSUMPTIONS (not in the rules): DexScreener has no 'prior hour' volume, so prior hourly avg is estimated from
(h6 - h1) spread over the token's earlier life; min 1h volume $3k and >=20 trades (noise floor); a 48h backstop exit
exists because the rules set no stop between Tier 1 and Tier 2.
ACCEL MODE (MEME_MODE=accel, own state file, runs alongside V5-1 to compare): Solana only. Entry, all must pass: age 15-90 min;
  liquidity >= $15k (no cap); 15-minute volume (change in DexScreener 24h volume between our own scans) >= 2x the previous 15-minute
  volume and >= $1k; mint AND freeze authority both revoked (read from a Solana RPC). No market-cap cap, no trade-count or buy/sell filter.
  Same exit ladder, but exit 100% if 2x is not reached within 45 minutes. NOT CHECKED: "top wallet is not the bonding curve"
  (needs holder data we do not have). Tokens need two scans before they can qualify. Candidates come only from DexScreener's
  latest-profiles/boosts lists, so fresh tokens without a profile or boost are never seen.
CAN'T DO: block-0 sniping or sub-minute reactions; this sees tokens only after they are listed."""
import json, os, time, traceback, urllib.parse, urllib.request

API = "https://api.dexscreener.com"
UA = {"User-Agent": "Mozilla/5.0 (compatible; meme-paper-scanner; educational)", "Accept": "application/json"}
STATE = "meme_state.json"
BANK, STAKE, MAX_POS = 1000.0, 20.0, 10
CHAINS = {"solana", "base", "bsc"}
MIN_LIQ, MAX_LIQ, MIN_AGE_H, MAX_AGE_H, MIN_VOL_H1, MAX_MCAP = 8_000, 40_000, 1.0, 4.0, 3_000, 300_000
SPIKE, BUY_SELL, MIN_TX = 3.0, 1.5, 20
STOP, T1, T2, TRAIL, MAX_HOLD_H, T1_DEADLINE_H = 0.25, 2.0, 4.0, 0.12, 48.0, 12.0
F_T1, F_T2 = 0.50, 0.30
RULES = 'V5-1'
SLIP_IN, SLIP_OUT, FEE, RUG_LIQ_DROP, RUG_PAYOUT = 0.03, 0.07, 0.01, 0.60, 0.30
COOLDOWN_H, SUMMARY_HOUR_UTC = 24.0, 8
ACCEL_NO4X_H = 0.0                 # ACCEL only: 2h no-4x exit is PAUSED (0 = off) at the user's request; set to 2.0 to re-enable
ACCEL_FORCE_CLOSE_BEFORE = 1791142122   # one-time (2026-10-04, user request): ACCEL positions opened before this moment are closed at the live price
TOKEN, CHAT = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")

# MEME_MODE=accel runs the "Acceleration Gate" variant (separate state file) next to the default V5 rules, to compare them.
MODE = os.environ.get("MEME_MODE", "v5")
LABEL = ""
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
RPC = os.environ.get("SOLANA_RPC") or PUBLIC_RPC   # set repo secret SOLANA_RPC to a keyed (e.g. Helius free) URL
ACCEL_AGE_MIN_H, ACCEL_AGE_MAX_H, ACCEL_MIN_LIQ, ACCEL_VEL, ACCEL_MIN_DELTA = 0.25, 1.5, 15_000, 2.0, 1_000
if MODE == "accel":
    STATE, CHAINS, T1_DEADLINE_H, RULES, LABEL = "meme_accel_state.json", {"solana"}, 0.75, "ACCEL-1", "[ACCEL] "
if MODE == "birth":      # Pump.fun launches from PumpPortal's free websocket, then the same exits as ACCEL
    STATE, CHAINS, T1_DEADLINE_H, RULES, LABEL = "meme_birth_state.json", {"solana"}, 0.75, "BIRTH-1", "[BIRTH] "
if MODE == "loose":      # research control: buy almost every Pump.fun launch (no age/score/volume/authority gates), small stakes
    STATE, CHAINS, T1_DEADLINE_H, RULES, LABEL = "meme_loose_state.json", {"solana"}, 0.75, "LOOSE-1", "[LOOSE] "
    STAKE, MAX_POS = 5.0, 60
LOOSE_MAX_AGE_MIN = float(os.environ.get("LOOSE_MAX_AGE_MIN", "5"))   # LOOSE = fresh launches only: never buy a token older than this
LOOSE_MIN_LIQ = 300.0        # technical floor only: below this a $5 buy is >1.6% of the pool and the price impact model dominates
STATE = os.environ.get("MEME_STATE_FILE") or STATE        # diagnostics can use a scratch state file


def get(path):
    last = None
    for i in range(3):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(API + path, headers=UA), timeout=30))
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{path}: {last}")


def tg(text):
    text = LABEL + text
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


RAMP_LIQ = 300_000      # entries with >= this much liquidity are tagged "ramp-bigliq" (2026-10-05): recycled big-pool symbols with smooth scripted ramps


def tag_of(liq):
    return "ramp-bigliq" if (liq or 0) >= RAMP_LIQ else "organic"


def load():
    s = json.load(open(STATE)) if os.path.exists(STATE) else {}
    s.setdefault("cash", BANK); s.setdefault("pos", []); s.setdefault("closed", [])
    if s.get("rules") != RULES:
        s["funnel"] = {}; s["rules"] = RULES
    s.setdefault("seen", {}); s.setdefault("snap", {}); s.setdefault("track", {}); s.setdefault("hot", {}); s.setdefault("scans", 0); s.setdefault("last_hb", 0); s.setdefault("funnel", {}); s.setdefault("last_summary", ""); s.setdefault("api_fail", 0)
    for x in s["pos"] + s["closed"]:                      # tag-only: also labels history; changes no trading behaviour
        if "tag" not in x:
            x["tag"] = tag_of(x.get("liq_entry"))
    return s


def best_pair(pairs, addr=None):
    pairs = [p for p in pairs if isinstance(p, dict) and f(p.get("priceUsd")) > 0]
    if addr:
        pairs = [p for p in pairs if (p.get("baseToken") or {}).get("address", "").lower() == addr.lower()]
    return max(pairs, key=lambda p: f((p.get("liquidity") or {}).get("usd")), default=None)


def sell(pos, tokens, price, s, why, rug=False):
    tokens = min(tokens, pos["tokens"])
    if tokens <= 0:
        return 0.0
    impact = STAKE / max(pos["liq_entry"], 1.0)
    if rug:
        got = tokens * price * RUG_PAYOUT
    else:
        got = tokens * price * (1 - SLIP_OUT - impact) * (1 - FEE)
    got = max(got, 0.0)
    pos["tokens"] -= tokens
    pos["proceeds"] += got
    pos["obs_sold"] = pos.get("obs_sold", 0.0) + tokens * price
    s["cash"] += got
    return got


def close_out(s, pos, why):
    pos["reason"], pos["closed"] = why, time.strftime("%F %T")
    pos["pnl"] = round(pos["proceeds"] - STAKE, 2)
    pos["mult_net"] = round(pos["proceeds"] / STAKE, 3)
    s["closed"].append(pos)
    s["pos"].remove(pos)
    tg(f"MEME PAPER CLOSE {pos['sym']} ({why}) | in ${STAKE:.0f} -> out ${pos['proceeds']:.2f} ({(pos['mult_net']-1)*100:+.0f}%) "
       f"| held {(time.time()-pos['t_open'])/3600:.1f}h")


def manage(s):
    for pos in list(s["pos"]):
        try:
            d = get(f"/latest/dex/pairs/{pos['chain']}/{pos['pair']}")
            p = best_pair(d.get("pairs") or d.get("pair") and [d["pair"]] or [])
        except Exception as e:
            print("price fetch failed:", pos["sym"], e)
            p = None
        if not p:
            pos["miss"] = pos.get("miss", 0) + 1
            if pos["miss"] >= 3:                       # pair vanished for ~45 min: treat as rugged/dead
                pos["tokens"] = 0
                close_out(s, pos, "vanished")
            continue
        pos["miss"] = 0
        price, liq = f(p["priceUsd"]), f((p.get("liquidity") or {}).get("usd"))
        pos["peak"] = max(pos["peak"], price)
        prev_liq = pos.get("liq_last", pos["liq_entry"])
        prev_price = pos.get("price_last", pos["entry_obs"])
        pos["price_last"] = price
        pos["liq_last"] = liq if liq > 0 else prev_liq        # a missing/zero liquidity field is not evidence of a rug by itself
        mult, age_h = price / pos["entry_obs"], (time.time() - pos["t_open"]) / 3600
        if MODE == "accel" and pos["t_open"] < ACCEL_FORCE_CLOSE_BEFORE:
            sell(pos, pos["tokens"], price, s, "manual-close")
            close_out(s, pos, "manual-close-2026-10-04")
            continue
        pos["last"], pos["last_mult"] = price, round(mult, 3)
        pos.setdefault("path", []).append([int(time.time()), price, liq])      # for replaying alternative exit rules later
        pos["path"] = pos["path"][-700:]
        if liq < prev_liq * (1 - RUG_LIQ_DROP):
            if price <= prev_price * 0.5:                    # real rugs seen so far: liquidity -90% AND price -98% in one step
                sell(pos, pos["tokens"], price, s, "rug", rug=True)
                close_out(s, pos, "rug-liquidity-collapse")
                continue
            funnel(s, "liq_glitch")                          # liquidity dropped but price did not: data glitch (was booked as a rug before 2026-10-04)
        if not pos["t1"] and mult >= T1:
            pos["t1"] = True
            pos["t1_time"] = time.time()
            got = sell(pos, pos["tokens0"] * F_T1, price, s, "t1")
            tg(f"MEME {pos['sym']} hit {T1:.0f}x: sold 50% (+${got:.2f})")
        if pos["t1"] and not pos["t2"] and mult >= T2 and pos["tokens"] > 0:
            pos["t2"] = True
            pos["peak"] = price                      # trailing stop starts from the 4x level
            got = sell(pos, pos["tokens0"] * F_T2, price, s, "t2")
            tg(f"MEME {pos['sym']} hit {T2:.0f}x: sold 30% (+${got:.2f}), 20% moon bag on 12% trail")
        why = None
        if not pos["t1"] and mult <= 1 - STOP:
            why = "stop"
        elif pos["t2"] and price <= pos["peak"] * (1 - TRAIL):
            why = "trail"
        elif not pos["t1"] and age_h >= T1_DEADLINE_H:
            why = "no-2x-deadline"
        elif MODE == "accel" and ACCEL_NO4X_H > 0 and pos["t1"] and not pos["t2"] and pos.get("t1_time") and time.time() - pos["t1_time"] >= ACCEL_NO4X_H * 3600:
            why = "no-4x-2h"
        elif age_h >= MAX_HOLD_H:
            why = "time"
        if why:
            sell(pos, pos["tokens"], price, s, why)
            close_out(s, pos, why)


def slots_used(s):
    """ACCEL: positions that already sold 50% at 2x (principal back, moon bag riding) do not use a slot, so a token stuck between
    2x and 4x cannot block new entries (user request 2026-10-04). Other modes count every open position."""
    if MODE == "accel":
        return sum(1 for p in s["pos"] if not p.get("t1"))
    return len(s["pos"])


def funnel(s, key, n=1):
    s["funnel"][key] = s["funnel"].get(key, 0) + n


GT_LAST = [0.0]


def get_url(url):
    """GeckoTerminal's free tier answers 429 when called too fast (seen in the diagnostic run): space calls >= 2.3 s apart
    and retry once after a pause."""
    gt = "geckoterminal" in url
    for attempt in range(2):
        if gt:
            time.sleep(max(0.0, GT_LAST[0] + 2.3 - time.time()))
            GT_LAST[0] = time.time()
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429 and gt and attempt == 0:
                time.sleep(6)
                continue
            raise


def raw_new_tokens(s):
    """ACCEL mode discovery: newest Solana pools straight from GeckoTerminal (free, no key) and, if reachable, Pump.fun's
    own coin list. Every source is counted in the funnel (xx_ok / xx_fail) so we can SEE which ones actually work from Actions."""
    out = {}
    for page in (1, 2):
        try:
            d = get_url(f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={page}")
            n = 0
            for r in d.get("data", []):
                bid = ((r.get("relationships") or {}).get("base_token") or {}).get("data", {}).get("id", "")
                if bid.startswith("solana_"):
                    out[("solana", bid[len("solana_"):])] = "geckoterminal"
                    n += 1
            funnel(s, "gt_ok" if n else "gt_empty")
        except Exception as e:
            funnel(s, "gt_fail")
            print("geckoterminal failed:", str(e)[:100])
    return out


def candidates(s=None):
    out = {}
    if MODE == "accel" and s is not None:
        out.update(raw_new_tokens(s))
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            for r in get(path):
                if isinstance(r, dict) and r.get("chainId") in CHAINS and r.get("tokenAddress"):
                    out[(r["chainId"], r["tokenAddress"])] = path.split("/")[1]
        except Exception as e:
            print("candidate source failed:", path, e)
    return out


def authorities_ok(mint):
    """Solana only. True if mint AND freeze authority are both revoked, False if either exists, None if unverifiable."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                       "params": [mint, {"encoding": "jsonParsed"}]}).encode()
    urls = [RPC] + ([PUBLIC_RPC] if RPC != PUBLIC_RPC else [])      # keyed RPC first; fall back if it is halted/blocked
    for url in urls:
        for i in range(2):
            try:
                r = json.load(urllib.request.urlopen(urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json", **UA}), timeout=20))
                info = r["result"]["value"]["data"]["parsed"]["info"]
                return info.get("mintAuthority") is None and info.get("freezeAuthority") is None
            except Exception as e:
                print("authority check failed:", url.split("?")[0][-30:], mint[:8], str(e)[:80])
                time.sleep(2)
    return None


SIM_SELL_MIN_RATIO = 0.95      # defense stack: the sell quote must return >= 95% of the DexScreener-implied value (user's setting)
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
WSOL = "So11111111111111111111111111111111111111112"


def simulate_sell(p, mint):
    """Jupiter sell quote for ~STAKE dollars of the token -> SOL. True = sellable at a sane price, False = no route / bad price,
    None = could not check. A quote is a proxy for a real sell simulation: it does not catch every honeypot."""
    try:
        price, pn = f(p.get("priceUsd")), f(p.get("priceNative"))
        if price <= 0 or pn <= 0:
            return None
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [mint, {"encoding": "jsonParsed"}]}).encode()
        dec = None
        for url in [RPC] + ([PUBLIC_RPC] if RPC != PUBLIC_RPC else []):
            try:
                r = json.load(urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **UA}), timeout=20))
                dec = int(r["result"]["value"]["data"]["parsed"]["info"]["decimals"]); break
            except Exception:
                continue
        if dec is None:
            return None
        tokens = STAKE / price
        q = json.load(urllib.request.urlopen(urllib.request.Request(
            JUP_QUOTE + "?" + urllib.parse.urlencode({"inputMint": mint, "outputMint": WSOL, "amount": int(tokens * 10 ** dec), "slippageBps": 1000}),
            headers=UA), timeout=20))
        out_sol = int(q["outAmount"]) / 1e9
        return out_sol >= SIM_SELL_MIN_RATIO * tokens * pn
    except urllib.error.HTTPError as e:
        return False if e.code in (400, 404) else None        # Jupiter answers 400/404 when there is no route
    except Exception as e:
        print("simulate_sell failed:", mint[:8], str(e)[:80])
        return None


def defense_stack(p, ch, mint, s):
    """Entry Defense Stack (V5 and ACCEL, Solana only): the Jupiter sell-quote check. (The 10 SOL rule was dropped: the $8k+
    liquidity floors are already stricter.) Fails open if Jupiter or the RPC cannot answer, and counts that in the funnel."""
    if ch != "solana":
        return True
    ok = simulate_sell(p, mint)
    if ok is False:
        funnel(s, "fail_sim_sell")
        return False
    if ok is None:
        funnel(s, "sim_sell_unavailable")
    else:
        funnel(s, "sim_sell_ok")
    return True


def velocity_calc(h, now, v24, age_h):
    """h = oldest-first list of [time, 24h volume] readings. Returns (recent, previous) volume in $ per 15 min, or None until a
    reading at least ~13 min old exists. recent = volume since the newest reading >=13 min old; previous = the window before that
    (or the token's lifetime average if there is no older reading). For tokens under 24h old, 24h volume is lifetime volume."""
    A = None
    for x in h:
        if now - x[0] >= 780:
            A = x
    if not A:
        return None
    recent = max(v24 - A[1], 0.0) / ((now - A[0]) / 900.0)
    B = None
    for x in h:
        if A[0] - x[0] >= 780:
            B = x
    if B:
        prev = max(A[1] - B[1], 0.0) / ((A[0] - B[0]) / 900.0)
    else:
        age_a = max(age_h - (now - A[0]) / 3600.0, 0.05)
        prev = A[1] / max(age_a * 4.0, 1.0)
    return (recent, prev)


def vol_velocity(s, addr, v24, age_h):
    """Track 24h volume locally at every scan; results as velocity_calc. Stores readings in s["snap"] (kept 1h)."""
    now = time.time()
    h = [x for x in (s["snap"].get(addr) or {}).get("h", []) if now - x[0] < 3600]
    out = velocity_calc(h, now, v24, age_h)
    h.append([now, v24])
    s["snap"][addr] = {"h": h, "age_h": age_h, "t": now}
    return out


GT_NET = {"solana": "solana", "base": "base", "bsc": "bsc"}
NEW_TRACKS = [0]                                   # new tracks started this scan (caps launch-price API calls)
VEL = {}                                           # address -> (recent, previous) 15-min volume, set by passes_accel


def launch_info(chain, pool):
    """First-trade price ('launch price'), peak and low so far, from GeckoTerminal 1-minute candles (free). None if unavailable.
    Candle history is capped at 1000 minutes, so for tokens older than ~16h the first candle is NOT the launch."""
    if not pool or chain not in GT_NET:
        return None
    try:
        d = get_url(f"https://api.geckoterminal.com/api/v2/networks/{GT_NET[chain]}/pools/{pool}/ohlcv/minute"
                    "?aggregate=1&limit=1000&currency=usd")
        rows = [r for r in d["data"]["attributes"]["ohlcv_list"] if r and f(r[1]) > 0]
        if not rows:
            return None
        first = min(rows, key=lambda r: r[0])
        return {"launch_price": f(first[1]), "first_trade_ts": int(first[0]), "ath": max(f(r[2]) for r in rows),
                "low": min(f(r[3]) for r in rows if f(r[3]) > 0), "candles": len(rows)}
    except Exception as e:
        print("launch info failed:", str(e)[:80])
        return None


def entry_context(p, price, fill, liq, launch, vel):
    ch, tx = p.get("priceChange") or {}, p.get("txns") or {}
    created = f(p.get("pairCreatedAt")) / 1000
    age_min = (time.time() - created) / 60 if created else -1
    mcap = f(p.get("marketCap")) or f(p.get("fdv"))
    L = [f"Entry price (observed): ${price:.8g} | assumed fill after {SLIP_IN*100:.0f}% slippage + impact: ${fill:.8g}",
         f"Age {age_min:.0f} min | market cap ${mcap:,.0f} | liquidity ${liq:,.0f}"]
    if launch and launch["launch_price"] > 0:
        lp, ath = launch["launch_price"], max(launch["ath"], price)
        L.append(f"Launch (first-trade) price ${lp:.8g} -> entry is {price/lp:.1f}x launch | peak so far {ath/lp:.1f}x launch | "
                 f"entry is {(1-price/ath)*100:.0f}% below that peak")
    else:
        L.append("Launch price: unavailable")
    L.append(f"Price change: 5m {f(ch.get('m5')):+.0f}% | 1h {f(ch.get('h1')):+.0f}%")
    L.append(f"Trades: 5m {f((tx.get('m5') or {}).get('buys')):.0f} buys / {f((tx.get('m5') or {}).get('sells')):.0f} sells | "
             f"1h {f((tx.get('h1') or {}).get('buys')):.0f} / {f((tx.get('h1') or {}).get('sells')):.0f}")
    if vel:
        L.append(f"Volume per 15 min: ${vel[0]:,.0f} now vs ${vel[1]:,.0f} before ({vel[0]/max(vel[1],1):.1f}x)")
    return "\n".join(L), {"mcap": mcap, "age_min": round(age_min, 1), "launch": launch,
                          "vel": list(vel) if vel else None}


TRACK_MAX_AGE_H, TRACK_START_AGE_H, TRACK_MIN_LIQ, TRACKS = 3.0, 1.5, 5_000, "meme_tracks.jsonl"


def track_update(s, a, p):
    """RESEARCH LOG (no effect on trading): record price/liquidity/volume of every young token from first sighting until it is
    3h old, so we can later replay 'what if we had entered at 15/30/45/60 min, with exit rule X' on real data."""
    now = time.time()
    created = f(p.get("pairCreatedAt")) / 1000
    age_h = (now - created) / 3600 if created else -1
    liq, price = f((p.get("liquidity") or {}).get("usd")), f(p.get("priceUsd"))
    tr = s["track"].get(a)
    if age_h <= 0 or price <= 0:
        return
    if tr is None:
        if age_h > TRACK_START_AGE_H or liq < TRACK_MIN_LIQ or len(s["track"]) >= 150 or NEW_TRACKS[0] >= 3:
            return
        NEW_TRACKS[0] += 1
        li = launch_info("solana", p.get("pairAddress"))
        tr = s["track"][a] = {"sym": (p["baseToken"].get("symbol") or "?")[:12], "addr": p["baseToken"]["address"],
                              "created": int(created), "launch_price": (li or {}).get("launch_price"),
                              "first_trade_ts": (li or {}).get("first_trade_ts"), "pts": []}
    if age_h <= TRACK_MAX_AGE_H:
        tr["pts"].append([int(now), price, liq, f((p.get("volume") or {}).get("h24"))])


def archive_tracks(s):
    now, done = time.time(), []
    for a, tr in s["track"].items():
        last = tr["pts"][-1][0] if tr["pts"] else tr["created"]
        if (now - tr["created"]) / 3600 > TRACK_MAX_AGE_H or now - last > 3600:
            done.append(a)
    if done:
        with open(TRACKS, "a") as fh:
            for a in done:
                fh.write(json.dumps(s["track"].pop(a)) + "\n")


def passes_accel(p, s, addr):
    liq = f((p.get("liquidity") or {}).get("usd"))
    created = f(p.get("pairCreatedAt")) / 1000
    age_h = (time.time() - created) / 3600 if created else -1
    if age_h <= 0 or age_h > ACCEL_AGE_MAX_H + 0.5:
        return False                                            # too old to ever qualify: do not even track it
    vel = vol_velocity(s, addr, f((p.get("volume") or {}).get("h24")), age_h)
    if not (ACCEL_AGE_MIN_H <= age_h <= ACCEL_AGE_MAX_H):
        funnel(s, "fail_age"); return False
    if liq < ACCEL_MIN_LIQ:
        funnel(s, "fail_liq"); return False
    if vel is None:
        funnel(s, "wait_second_reading"); return False
    rate, prev = vel
    if rate < ACCEL_MIN_DELTA or rate < ACCEL_VEL * max(prev, 1.0):
        funnel(s, "fail_velocity"); return False
    VEL[addr] = (rate, prev)
    ok = authorities_ok(p["baseToken"]["address"])
    if ok is None:
        funnel(s, "rpc_error"); return False
    if not ok:
        funnel(s, "fail_authority"); return False
    return True


def passes(p, s):
    liq = f((p.get("liquidity") or {}).get("usd"))
    created = f(p.get("pairCreatedAt")) / 1000
    age_h = (time.time() - created) / 3600 if created else -1
    v = p.get("volume") or {}
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = f(tx.get("buys")), f(tx.get("sells"))
    mcap = f(p.get("marketCap")) or f(p.get("fdv"))
    h1 = f(v.get("h1"))
    prior = max((f(v.get("h6")) - h1) / min(5.0, max(age_h - 1.0, 0.5)), 1.0)
    checks = [("liq", MIN_LIQ <= liq <= MAX_LIQ), ("age", MIN_AGE_H <= age_h <= MAX_AGE_H),
              ("mcap", 0 < mcap < MAX_MCAP),
              ("volume", h1 >= MIN_VOL_H1 and h1 >= SPIKE * prior),
              ("flow", buys + sells >= MIN_TX and buys >= BUY_SELL * max(sells, 1))]
    for name, ok in checks:
        if not ok:
            funnel(s, "fail_" + name)
            return False
    return True


def open_pos(s, p, ch, a, src, birth=None, feats=None):
    """Open one paper position (all modes). birth = dict from PumpPortal (exact launch data) in BIRTH mode."""
    price, liq = f(p["priceUsd"]), f((p.get("liquidity") or {}).get("usd"))
    fill = price * (1 + SLIP_IN + STAKE / max(liq, 1.0))
    tokens = STAKE * (1 - FEE) / fill
    s["cash"] -= STAKE
    s["seen"][a.lower()] = time.time()
    launch = ({} if feats is not None else launch_info(ch, p.get("pairAddress"))) or {}
    extra = ""
    if birth:
        pn = f(p.get("priceNative"))
        usd_per_sol = price / pn if pn > 0 else 0.0
        if birth.get("lp", 0) > 0 and usd_per_sol > 0:
            launch["launch_price"] = birth["lp"] * usd_per_sol           # exact bonding-curve price at creation, in USD
            launch["ath"] = max(launch.get("ath", 0.0), price)
            launch["launch_sol"] = birth["lp"]
        extra = (f"\nPump.fun launch: creator {str(birth.get('cr'))[:6]}.. bought {birth.get('ib', 0):.2f} SOL at creation | "
                 f"launch price {birth.get('lp', 0):.4g} SOL | launch mcap {birth.get('m0', 0):.0f} SOL")
    ctx_text, ctx = entry_context(p, price, fill, liq, launch if launch.get("launch_price") else None, VEL.get(a))
    ctx["birth"] = birth
    if feats is not None:
        ctx["feats"] = feats
        feats["x_launch"] = round(price / launch["launch_price"], 2) if launch.get("launch_price") else None
        ctx_text, extra = "", ""
    s["pos"].append({"ctx": ctx, "sym": (p["baseToken"].get("symbol") or "?")[:12], "addr": p["baseToken"]["address"],
                     "chain": ch, "pair": p["pairAddress"], "entry_obs": price, "fill": fill, "liq_entry": liq,
                     "tokens0": tokens, "tokens": tokens, "proceeds": 0.0, "peak": price, "t1": False, "t2": False,
                     "t_open": time.time(), "opened": time.strftime("%F %T"), "src": src or "?", "tag": tag_of(liq),
                     "h1_change": f((p.get("priceChange") or {}).get("h1")), "miss": 0})
    funnel(s, "opened")
    if feats is not None:
        au = feats.get("authority_ok")
        tg(f"MEME PAPER BUY {p['baseToken'].get('symbol')} ${STAKE:.0f} | age {feats['age_min']:.0f}m | liq ${liq:,.0f} | "
           f"mcap ${ctx['mcap']:,.0f} | {feats['x_launch'] or '?'}x launch | authority "
           f"{'revoked' if au else ('NOT revoked' if au is False else 'unknown')} | creator bought {(birth or {}).get('ib', 0):.2f} SOL\n"
           f"{p.get('url', '')}")
        return
    tg(f"MEME PAPER BUY {p['baseToken'].get('symbol')} on {ch} ${STAKE:.0f} (source {src})\n{ctx_text}{extra}\n{p.get('url', '')}")


def scan(s):
    held = {x["addr"].lower() for x in s["pos"]}
    cand = candidates(s)
    NEW_TRACKS[0] = 0
    if MODE == "accel":
        for tr in s["track"].values():
            cand.setdefault(("solana", tr["addr"]), "track")
    funnel(s, "candidates", len(cand))
    by_chain = {}
    for (ch, addr), src in cand.items():
        if addr.lower() not in held and time.time() - s["seen"].get(addr.lower(), 0) > COOLDOWN_H * 3600:
            by_chain.setdefault(ch, []).append((addr, src))
    for ch, items in by_chain.items():
        for i in range(0, len(items), 30):
            chunk = items[i:i + 30]
            try:
                pairs = get(f"/tokens/v1/{ch}/{','.join(a for a, _ in chunk)}")
            except Exception as e:
                print("pairs fetch failed:", e)
                continue
            src_of = {a.lower(): src for a, src in chunk}
            groups = {}
            for p in pairs if isinstance(pairs, list) else []:
                a = ((p.get("baseToken") or {}).get("address") or "").lower()
                if a:
                    groups.setdefault(a, []).append(p)
            for a, plist in groups.items():
                p = best_pair(plist)
                if not p:
                    continue
                funnel(s, "evaluated")
                if MODE == "accel":
                    track_update(s, a, p)
                if MODE != "accel" and (len(s["pos"]) >= MAX_POS or s["cash"] < STAKE):
                    funnel(s, "no_slot")
                    continue
                if MODE == "accel":
                    if slots_used(s) < MAX_POS and s["cash"] >= STAKE and not passes_accel(p, s, a):
                        continue
                    elif slots_used(s) >= MAX_POS or s["cash"] < STAKE:
                        continue
                elif not passes(p, s):
                    continue
                if MODE in ("v5", "accel") and not defense_stack(p, ch, a, s):
                    continue
                open_pos(s, p, ch, a, src_of.get(a))
    if MODE == "accel":
        archive_tracks(s)
    s["snap"] = {k: v for k, v in s["snap"].items() if time.time() - v["t"] < 3 * 3600}
    cut = time.time() - 7 * 86400
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cut}


# ---------------------------------------------------------------- BIRTH mode (PumpPortal websocket) ----------------------------
import threading
WS_URL = "wss://pumpportal.fun/api/data"
BIRTHS_FILE = "meme_births.json"                   # NOT committed; handed between jobs with actions/cache
BIRTHS, LOCK, WS_COUNT, WS_LAST = {}, threading.Lock(), [0], [0]
BIRTH_KEEP_MIN, SCREEN_MIN, SCREEN_MAX, SCREEN_EVERY, SCREEN_CAP, HOT_LIQ = 105, 10, 100, 300, 1500, 8_000
BIRTH_AGE_MIN, BIRTH_AGE_MAX, BIRTH_MAX_STORED = 15.0, 90.0, 6000


def births_load():
    try:
        d = json.load(open(BIRTHS_FILE))
        with LOCK:
            for m, b in d.items():
                BIRTHS.setdefault(m, b)
        print("births loaded:", len(d))
    except Exception as e:
        print("no births file:", str(e)[:60])


def births_save():
    with LOCK:
        snap = {m: b for m, b in BIRTHS.items()}
    tmp = BIRTHS_FILE + ".tmp"
    json.dump(snap, open(tmp, "w"), separators=(",", ":"))
    os.replace(tmp, BIRTHS_FILE)


def ws_loop():
    import asyncio, websockets

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
                        vt = f(d.get("vTokensInBondingCurve"))
                        if d.get("txType") == "create" and d.get("mint") and vt > 0:
                            with LOCK:
                                BIRTHS[d["mint"]] = {"t": time.time(), "lp": f(d.get("vSolInBondingCurve")) / vt,
                                                     "cr": d.get("traderPublicKey"), "ib": f(d.get("solAmount")),
                                                     "m0": f(d.get("marketCapSol")), "h": [], "la": 0}
                            WS_COUNT[0] += 1
            except Exception as e:
                print("ws error:", str(e)[:100], "- reconnecting in", wait, "s")
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30)

    asyncio.run(run())


def births_start():
    births_load()
    threading.Thread(target=ws_loop, daemon=True).start()


def passes_birth(p, s, m, age_h, vel):
    liq = f((p.get("liquidity") or {}).get("usd"))
    if not (BIRTH_AGE_MIN <= age_h * 60 <= BIRTH_AGE_MAX):
        funnel(s, "fail_age"); return False
    if liq < ACCEL_MIN_LIQ:
        funnel(s, "fail_liq"); return False
    if vel is None:
        funnel(s, "wait_second_reading"); return False
    rate, prev = vel
    if rate < ACCEL_MIN_DELTA or rate < ACCEL_VEL * max(prev, 1.0):
        funnel(s, "fail_velocity"); return False
    VEL[m.lower()] = (rate, prev)
    ok = authorities_ok(m)
    if ok is None:
        funnel(s, "rpc_error"); return False
    if not ok:
        funnel(s, "fail_authority"); return False
    return True


def scan_birth(s):
    now = time.time()
    delta = WS_COUNT[0] - WS_LAST[0]
    WS_LAST[0] = WS_COUNT[0]
    if delta > 0:
        funnel(s, "ws_births", delta)
    with LOCK:
        for m in [m for m, b in BIRTHS.items() if now - b["t"] > BIRTH_KEEP_MIN * 60]:
            del BIRTHS[m]
        if len(BIRTHS) > BIRTH_MAX_STORED:
            for m in sorted(BIRTHS, key=lambda k: BIRTHS[k]["t"])[:len(BIRTHS) - BIRTH_MAX_STORED]:
                del BIRTHS[m]
        births = dict(BIRTHS)
    held = {x["addr"] for x in s["pos"]}
    hot = s["hot"]
    for m in list(hot):
        if m not in births or (now - births[m]["t"]) / 60 > (min(SCREEN_MAX, LOOSE_MAX_AGE_MIN) if MODE == "loose" else SCREEN_MAX):
            del hot[m]
    screen = [m for m, b in births.items()
              if (1 if MODE == "loose" else SCREEN_MIN) <= (now - b["t"]) / 60 <= (min(SCREEN_MAX, LOOSE_MAX_AGE_MIN) if MODE == "loose" else SCREEN_MAX) and m not in hot and m not in held
              and now - b.get("la", 0) >= SCREEN_EVERY - 10 and now - s["seen"].get(m.lower(), 0) > COOLDOWN_H * 3600]
    screen.sort(key=lambda m: (births[m].get("la", 0), -births[m]["t"]))      # never-tried first, youngest first
    screen = screen[:SCREEN_CAP]
    for m in list(hot):
        if now - s["seen"].get(m.lower(), 0) <= COOLDOWN_H * 3600:       # already traded this token: never re-enter it
            del hot[m]
    look = [m for m in hot if m not in held] + screen
    funnel(s, "b_lookups", len(look))
    for i in range(0, len(look), 30):
        chunk = look[i:i + 30]
        for m in chunk:
            births[m]["la"] = now
        try:
            pairs = get(f"/tokens/v1/solana/{','.join(chunk)}")
        except Exception as e:
            print("pairs fetch failed:", e)
            continue
        groups = {}
        for p in pairs if isinstance(pairs, list) else []:
            m = (p.get("baseToken") or {}).get("address") or ""
            if m in births:
                groups.setdefault(m, []).append(p)
        for m, plist in groups.items():
            p = best_pair(plist)
            if not p:
                continue
            b = births[m]
            funnel(s, "evaluated")
            age_h = (now - b["t"]) / 3600
            if "fp" not in b:                                  # first time DexScreener lists this launch: how late is it?
                b["fp"] = round(age_h * 60, 1)
                funnel(s, "pair_first")
                funnel(s, "pair_age_" + ("<5m" if b["fp"] < 5 else "5-10m" if b["fp"] < 10 else "10-20m" if b["fp"] < 20
                                         else "20-40m" if b["fp"] < 40 else "40m+"))
            v24 = f((p.get("volume") or {}).get("h24"))
            vel = velocity_calc(b["h"], now, v24, age_h)
            b["h"] = [x for x in b["h"] if now - x[0] < 2700] + [[now, v24]]
            if f((p.get("liquidity") or {}).get("usd")) >= HOT_LIQ:
                if m not in hot:
                    funnel(s, "b_hot")
                hot[m] = hot.get(m, now)
            if len(s["pos"]) >= MAX_POS or s["cash"] < STAKE:
                continue
            if now - s["seen"].get(m.lower(), 0) <= COOLDOWN_H * 3600:
                continue
            feats = None
            if MODE == "loose":
                liq = f((p.get("liquidity") or {}).get("usd"))
                if age_h * 60 > LOOSE_MAX_AGE_MIN:
                    funnel(s, "fail_age_loose")
                    continue
                if liq < LOOSE_MIN_LIQ:
                    funnel(s, "fail_liq")
                    continue
                feats = {"age_min": round(age_h * 60, 1), "liq": round(liq), "authority_ok": authorities_ok(m),
                         "vel": list(vel) if vel else None, "creator_buy_sol": b.get("ib")}
            elif not passes_birth(p, s, m, age_h, vel):
                continue
            open_pos(s, p, "solana", m, "pumpportal", birth={k: b[k] for k in ("t", "lp", "cr", "ib", "m0")}, feats=feats)
            held.add(m)
    with LOCK:
        for m, b in births.items():
            if m in BIRTHS:
                BIRTHS[m]["h"], BIRTHS[m]["la"] = b["h"], b["la"]
                if "fp" in b:
                    BIRTHS[m]["fp"] = b["fp"]
    births_save()
    s["seen"] = {k: v for k, v in s["seen"].items() if v > now - 7 * 86400}


def net_mult(t, slip_mult):
    """Re-price a closed trade as if exit slippage had been slip_mult x worse (rugs/vanished use actual proceeds)."""
    if t.get("reason", "").startswith("rug") or t.get("reason") == "vanished":
        return t["proceeds"] / STAKE
    impact = STAKE / max(t["liq_entry"], 1.0)
    return t.get("obs_sold", 0.0) * (1 - SLIP_OUT * slip_mult - impact) * (1 - FEE) / STAKE


def summary(s):
    c = s["closed"]
    eq = s["cash"] + sum(x["tokens"] * x.get("last", x["entry_obs"]) * (1 - SLIP_OUT) * (1 - FEE) for x in s["pos"])
    L = [f"Meme paper scanner | equity ${eq:,.2f} (start ${BANK:,.0f}) | cash ${s['cash']:,.2f} | open {len(s['pos'])} | closed {len(c)}"]
    if c:
        w = sum(1 for t in c if t["pnl"] > 0)
        m = sorted(t["mult_net"] for t in c)
        L.append(f"Win rate {w}/{len(c)} | median {m[len(m)//2]:.2f}x | best {m[-1]:.2f}x | worst {m[0]:.2f}x | "
                 f"avg {sum(m)/len(m):.2f}x")
        for k in (1, 2, 3):
            L.append(f"  if exit slippage were {k}x: avg {sum(net_mult(t, k) for t in c)/len(c):.2f}x per trade")
        rug = sum(1 for t in c if t["reason"].startswith("rug") or t["reason"] == "vanished")
        L.append(f"Rugs/vanished: {rug} of {len(c)}")
    if MODE == "accel":
        for tg_name in ("organic", "ramp-bigliq"):
            g = [x for x in c if x.get("tag") == tg_name]
            o = sum(1 for x in s["pos"] if x.get("tag") == tg_name)
            if g:
                gm = sorted(x["mult_net"] for x in g)
                L.append(f"  [{tg_name}] closed {len(g)} | win {sum(1 for x in g if x['pnl'] > 0)}/{len(g)} | median {gm[len(gm)//2]:.2f}x | avg {sum(gm)/len(gm):.2f}x | open {o}")
            else:
                L.append(f"  [{tg_name}] closed 0 | open {o}")
    fu = s["funnel"]
    L.append("Why no trades: " + ", ".join(f"{k.replace('fail_', '')} {v}" for k, v in sorted(fu.items()) if k.startswith(("fail_", "wait_", "rpc_", "gt_", "pf_", "b_", "ws_", "pair_")))
             + f" | seen {fu.get('candidates', 0) or fu.get('ws_births', 0)} tokens, {fu.get('opened', 0)} opened")
    if MODE == "loose" and c:
        def grp(name, key):
            g = {}
            for tr in c:
                g.setdefault(key(tr), []).append(tr["mult_net"])
            return name + ": " + " | ".join(f"{k}: n={len(v)} avg {sum(v)/len(v):.2f}x win {100*sum(1 for y in v if y > 1)//len(v)}%"
                                            for k, v in sorted(g.items(), key=lambda kv: str(kv[0])))
        fe = lambda tr: (tr.get("ctx") or {}).get("feats") or {}
        L.append(grp("By authority", lambda tr: {True: "revoked", False: "NOT revoked", None: "unknown"}[fe(tr).get("authority_ok")]))
        L.append(grp("By age at entry", lambda tr: "<10m" if fe(tr).get("age_min", 99) < 10 else ("10-30m" if fe(tr).get("age_min", 99) < 30 else "30m+")))
        L.append(grp("By liquidity", lambda tr: "<$2k" if fe(tr).get("liq", 0) < 2000 else ("$2-10k" if fe(tr).get("liq", 0) < 10000 else "$10k+")))
    for x in s["pos"][:15]:
        L.append(f"  {x['sym']} ({x['chain']}) {x.get('last_mult', 1):.2f}x")
    return "\n".join(L)


def main():
    s = load()
    try:
        manage(s)
        scan_birth(s) if MODE in ("birth", "loose") else scan(s)
        s["scans"] += 1
        s["api_fail"] = 0
    except RuntimeError as e:
        s["api_fail"] += 1
        print("api problem:", e)
        if s["api_fail"] == 8:
            tg("Meme scanner: DexScreener has failed for 8 runs in a row. " + str(e)[:200])
    if time.time() - s["last_hb"] > 3 * 3600:               # proof of life in Telegram every ~3h even when nothing trades
        s["last_hb"] = time.time()
        fu = s["funnel"]
        tg(f"alive: {s['scans']} scans so far | seen {fu.get('candidates', fu.get('ws_births', 0))} tokens | opened "
           f"{fu.get('opened', 0)} | open now {len(s['pos'])} | closed {len(s['closed'])}")
    now = time.gmtime()
    today = time.strftime("%F", now)
    if now.tm_hour >= SUMMARY_HOUR_UTC and s["last_summary"] != today:
        s["last_summary"] = today
        tg("Daily meme summary\n" + summary(s))
    s["cash"] = round(s["cash"], 4)
    json.dump(s, open(STATE, "w"), indent=1)
    print(summary(s).splitlines()[0])


if __name__ == "__main__":
    # LOOP_MIN>0: keep scanning every SCAN_SEC seconds for that many minutes (GitHub's cron is too unreliable for 15-min scans).
    loop_min = float(os.environ.get("LOOP_MIN") or 0)
    scan_sec = float(os.environ.get("SCAN_SEC") or 300)
    end, crashed = time.time() + loop_min * 60, False
    if MODE in ("birth", "loose"):
        births_start()
        time.sleep(3)
    while True:
        t0 = time.time()
        try:
            main()
        except Exception:
            if loop_min <= 0:
                tg("Meme scanner crashed:\n" + traceback.format_exc()[-1200:])
                raise
            print("scan failed:", traceback.format_exc()[-600:])
            if not crashed:
                crashed = True
                tg("Meme scanner scan error (will keep looping):\n" + traceback.format_exc()[-800:])
        if time.time() + scan_sec >= end:
            break
        time.sleep(max(scan_sec - (time.time() - t0), 5))
