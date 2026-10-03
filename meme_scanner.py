"""Meme-token scanner + PAPER simulator. FAKE money only. No wallet, no keys, no orders, ever.
Data: DexScreener public API (free, no key). Runs every ~15 minutes on GitHub Actions.
Entry (my assumptions, adjust MEME_* constants): confirmed early momentum on liquid, 1-72h-old pairs:
  1h volume >= 3x its 24h hourly average, 1h price +10%..+100%, buyers clearly outnumber sellers, not already parabolic.
Exits (trend-following, paper): stop -25%; sell 50% at 2x; sell 25% at 4x; trail the rest 12% from its peak; 48h time stop.
Pessimistic costs: entry slippage 3% + impact, exit slippage 7% + impact, 1% fee per side. Exits execute at the
price observed AFTER a breach (15-minute gaps), never at the stop level. Liquidity collapse = rug: exit at 30% of value.
CAN'T DO: block-0 sniping or sub-minute reactions; this sees tokens only after they trend."""
import json, os, time, traceback, urllib.parse, urllib.request

API = "https://api.dexscreener.com"
UA = {"User-Agent": "Mozilla/5.0 (compatible; meme-paper-scanner; educational)", "Accept": "application/json"}
STATE = "meme_state.json"
BANK, STAKE, MAX_POS = 1000.0, 20.0, 10
CHAINS = {"solana", "base", "bsc"}
MIN_LIQ, MIN_AGE_H, MAX_AGE_H, MIN_VOL_H1 = 50_000, 1.0, 72.0, 20_000
SPIKE, CH_LO, CH_HI, MAX_CH24, BUY_SELL, MIN_TX = 3.0, 10.0, 100.0, 400.0, 1.3, 100
STOP, T1, T2, TRAIL, MAX_HOLD_H = 0.25, 2.0, 4.0, 0.12, 48.0
SLIP_IN, SLIP_OUT, FEE, RUG_LIQ_DROP, RUG_PAYOUT = 0.03, 0.07, 0.01, 0.60, 0.30
COOLDOWN_H, SUMMARY_HOUR_UTC = 24.0, 8
TOKEN, CHAT = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


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
    s.setdefault("cash", BANK); s.setdefault("pos", []); s.setdefault("closed", [])
    s.setdefault("seen", {}); s.setdefault("funnel", {}); s.setdefault("last_summary", ""); s.setdefault("api_fail", 0)
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
        mult, age_h = price / pos["entry_obs"], (time.time() - pos["t_open"]) / 3600
        pos["last"], pos["last_mult"] = price, round(mult, 3)
        if liq < pos["liq_entry"] * (1 - RUG_LIQ_DROP):
            sell(pos, pos["tokens"], price, s, "rug", rug=True)
            close_out(s, pos, "rug-liquidity-collapse")
            continue
        if not pos["t1"] and mult >= T1:
            pos["t1"] = True
            got = sell(pos, pos["tokens0"] * 0.5, price, s, "t1")
            tg(f"MEME {pos['sym']} hit {T1:.0f}x: sold half (+${got:.2f}), trailing the rest")
        if pos["t1"] and not pos["t2"] and mult >= T2 and pos["tokens"] > 0:
            pos["t2"] = True
            got = sell(pos, pos["tokens0"] * 0.25, price, s, "t2")
            tg(f"MEME {pos['sym']} hit {T2:.0f}x: sold another quarter (+${got:.2f})")
        why = None
        if not pos["t1"] and mult <= 1 - STOP:
            why = "stop"
        elif pos["t1"] and price <= pos["peak"] * (1 - TRAIL):
            why = "trail"
        elif age_h >= MAX_HOLD_H:
            why = "time"
        if why:
            sell(pos, pos["tokens"], price, s, why)
            close_out(s, pos, why)


def funnel(s, key, n=1):
    s["funnel"][key] = s["funnel"].get(key, 0) + n


def candidates():
    out = {}
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            for r in get(path):
                if isinstance(r, dict) and r.get("chainId") in CHAINS and r.get("tokenAddress"):
                    out[(r["chainId"], r["tokenAddress"])] = path.split("/")[1]
        except Exception as e:
            print("candidate source failed:", path, e)
    return out


def passes(p, s):
    liq = f((p.get("liquidity") or {}).get("usd"))
    created = f(p.get("pairCreatedAt")) / 1000
    age_h = (time.time() - created) / 3600 if created else -1
    v = p.get("volume") or {}
    tx = (p.get("txns") or {}).get("h1") or {}
    ch = p.get("priceChange") or {}
    buys, sells = f(tx.get("buys")), f(tx.get("sells"))
    checks = [("liq", liq >= MIN_LIQ), ("age", MIN_AGE_H <= age_h <= MAX_AGE_H),
              ("volume", f(v.get("h1")) >= MIN_VOL_H1 and f(v.get("h1")) >= SPIKE * max(f(v.get("h24")) / 24, 1.0)),
              ("trend", CH_LO <= f(ch.get("h1")) <= CH_HI and f(ch.get("h24")) <= MAX_CH24),
              ("flow", buys + sells >= MIN_TX and buys >= BUY_SELL * max(sells, 1))]
    for name, ok in checks:
        if not ok:
            funnel(s, "fail_" + name)
            return False
    return True


def scan(s):
    held = {x["addr"].lower() for x in s["pos"]}
    cand = candidates()
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
                if len(s["pos"]) >= MAX_POS or s["cash"] < STAKE:
                    funnel(s, "no_slot")
                    continue
                if not passes(p, s):
                    continue
                price, liq = f(p["priceUsd"]), f((p.get("liquidity") or {}).get("usd"))
                fill = price * (1 + SLIP_IN + STAKE / max(liq, 1.0))
                tokens = STAKE * (1 - FEE) / fill
                s["cash"] -= STAKE
                s["seen"][a] = time.time()
                s["pos"].append({"sym": (p["baseToken"].get("symbol") or "?")[:12], "addr": p["baseToken"]["address"],
                                 "chain": ch, "pair": p["pairAddress"], "entry_obs": price, "fill": fill, "liq_entry": liq,
                                 "tokens0": tokens, "tokens": tokens, "proceeds": 0.0, "peak": price, "t1": False, "t2": False,
                                 "t_open": time.time(), "opened": time.strftime("%F %T"), "src": src_of.get(a, "?"),
                                 "h1_change": f((p.get("priceChange") or {}).get("h1")), "miss": 0})
                funnel(s, "opened")
                tg(f"MEME PAPER BUY {p['baseToken'].get('symbol')} on {ch} ${STAKE:.0f} @ ${price:.8g} | liq ${liq:,.0f} | "
                   f"1h {f((p.get('priceChange') or {}).get('h1')):+.0f}% | source {src_of.get(a)}\n{p.get('url', '')}")
    cut = time.time() - 7 * 86400
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cut}


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
    fu = s["funnel"]
    L.append("Why no trades: " + ", ".join(f"{k.replace('fail_', '')} {v}" for k, v in sorted(fu.items()) if k.startswith("fail_"))
             + f" | seen {fu.get('candidates', 0)} candidates, {fu.get('opened', 0)} opened")
    for x in s["pos"]:
        L.append(f"  {x['sym']} ({x['chain']}) {x.get('last_mult', 1):.2f}x")
    return "\n".join(L)


def main():
    s = load()
    try:
        manage(s)
        scan(s)
        s["api_fail"] = 0
    except RuntimeError as e:
        s["api_fail"] += 1
        print("api problem:", e)
        if s["api_fail"] == 8:
            tg("Meme scanner: DexScreener has failed for 8 runs in a row. " + str(e)[:200])
    now = time.gmtime()
    today = time.strftime("%F", now)
    if now.tm_hour >= SUMMARY_HOUR_UTC and s["last_summary"] != today:
        s["last_summary"] = today
        tg("Daily meme summary\n" + summary(s))
    s["cash"] = round(s["cash"], 4)
    json.dump(s, open(STATE, "w"), indent=1)
    print(summary(s).splitlines()[0])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tg("Meme scanner crashed:\n" + traceback.format_exc()[-1200:])
        raise
