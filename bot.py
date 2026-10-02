"""Paper-trading bot for Polymarket. FAKE money only. No wallet, no trading keys.
Each run: settle finished markets, scan active ones, open simulated trades,
answer Telegram commands, and send alerts. State lives in state.json."""
import json, math, os, re, time, datetime, urllib.request, urllib.parse, traceback

LIST = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
        "&limit=200&order=volume24hr&ascending=false")
ONE = "https://gamma-api.polymarket.com/markets/"
STATE = "state.json"
START = 50.0        # fake starting bankroll
MAX_FRAC = 0.06     # max 6% of equity per trade
MIN_EDGE = 0.02     # min gap between fair value and (haircut) entry price
MIN_LIQ = 5000      # skip thin markets
HAIRCUT = 0.01      # pessimistic fill: pay 1 cent worse than the quoted price
MAX_PER_EVENT = 2   # cap correlated bets on the same event
VOID_WAIT = 86400   # seconds a market may sit closed-but-undecided before refund
SUMMARY_HOUR_UTC = 8
VOL_INFLATE = 1.25  # fat-tail allowance: real Bitcoin moves exceed the plain bell-curve estimate

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-bot"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def tg(text):
    print(text)
    if not (TOKEN and CHAT):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data), timeout=20)
    except Exception as e:
        print("telegram send failed:", e)


def jl(x):
    return json.loads(x) if isinstance(x, str) else x


def prices(m):
    return [float(x) for x in jl(m.get("outcomePrices"))]


def is_yes_no(m):
    try:
        return [str(o).lower() for o in jl(m.get("outcomes"))] == ["yes", "no"]
    except Exception:
        return False


def fair_yes(m, p):
    """THE ONLY PART THAT MATTERS. Your estimate of the true YES probability.
    Placeholder = favorite-longshot bias. UNPROVEN. Replace with a better idea."""
    if p < 0.08:
        return p * 0.6
    if p > 0.92:
        return min(0.99, p + 0.03)
    return p


# ---------- information edge: live Bitcoin price model ----------
BTC_Q = re.compile(r"price of bitcoin be above \$?([\d,]+(?:\.\d+)?)", re.I)
_BTC = {}


def Phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def btc_context():
    """Live BTC spot (Coinbase) + 30-day daily volatility, fetched once per run."""
    if "ctx" in _BTC:
        return _BTC["ctx"]
    try:
        spot = float(get("https://api.coinbase.com/v2/prices/BTC-USD/spot")["data"]["amount"])
        c = sorted(get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"),
                   key=lambda x: x[0])[:-1]            # drop today's partial candle
        closes = [x[4] for x in c][-31:]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        mu = sum(rets) / len(rets)
        sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        _BTC["ctx"] = (spot, sd)
    except Exception as e:
        print("btc data failed:", e)
        _BTC["ctx"] = None
    return _BTC["ctx"]


def btc_model(m):
    """P(BTC finishes above strike) from live spot, vol and time left. None if not a BTC-threshold market."""
    mt = BTC_Q.search(m.get("question", ""))
    end = m.get("endDate")
    if not mt or not end:
        return None
    ctx = btc_context()
    if not ctx:
        return None
    try:
        if len(end) <= 10:
            end += "T16:00:00+00:00"                    # noon US Eastern
        T = (datetime.datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() - time.time()) / 86400
        K = float(mt.group(1).replace(",", ""))
        S, sd = ctx
        s_ = sd * VOL_INFLATE * math.sqrt(T)
        if T <= 0 or s_ <= 0:
            return None
        return Phi((math.log(S / K) - 0.5 * s_ * s_) / s_)
    except Exception:
        return None


def load():
    if os.path.exists(STATE):
        s = json.load(open(STATE))
    else:
        s = {"cash": START, "open": [], "closed": [], "dead": False}
    s.setdefault("offset", 0)
    s.setdefault("last_summary", "")
    s.setdefault("dead_notified", False)
    return s


def equity(s):
    """Cash + open positions marked to the latest market price (cost if unknown)."""
    return s["cash"] + sum(t["shares"] * t.get("mark", t["price"]) for t in s["open"])


def status(s):
    wins = sum(1 for t in s["closed"] if t["won"])
    lines = [f"Equity ${equity(s):.2f} (start ${START:.0f}) | cash ${s['cash']:.2f}",
             f"Open {len(s['open'])} | settled {len(s['closed'])} (wins {wins})"]
    for t in s["open"]:
        pnl = t["shares"] * t.get("mark", t["price"]) - t["cost"]
        lines.append(f"- {t['side']} ${t['cost']:.2f} @ {t['price']} | {pnl:+.2f} | {t['q'][:50]}")
    return "\n".join(lines)


def settle(s):
    keep = []
    for t in s["open"]:
        try:
            m = get(ONE + str(t["id"]))
            pr = prices(m)
        except Exception:
            keep.append(t)
            continue
        idx = 0 if t["side"] == "YES" else 1
        t["mark"] = round(pr[idx], 3)
        payout = None
        if m.get("closed") and max(pr) >= 0.99:
            payout = t["shares"] if pr[idx] >= 0.99 else 0.0
        elif m.get("closed"):
            # closed but no clear winner (void / 50-50). Refund at final prices after a wait.
            t.setdefault("closed_seen", time.time())
            if time.time() - t["closed_seen"] > VOID_WAIT:
                payout = t["shares"] * pr[idx]
        if payout is None:
            keep.append(t)
            continue
        s["cash"] += payout
        won = payout > t["cost"]
        t.update(payout=round(payout, 2), won=won, settled=time.strftime("%F %T"))
        s["closed"].append(t)
        tg(f"{'WIN' if won else 'LOSS'} {t['side']} | cost ${t['cost']:.2f} -> "
           f"${payout:.2f} ({payout - t['cost']:+.2f})\n{t['q']}")
    s["open"] = keep


def event_id(m):
    try:
        return str(m["events"][0]["id"])
    except Exception:
        return None


def scan(s):
    held = {t["id"] for t in s["open"]}
    per_event = {}
    for t in s["open"]:
        if t.get("ev"):
            per_event[t["ev"]] = per_event.get(t["ev"], 0) + 1
    for m in get(LIST):
        if m["id"] in held or float(m.get("liquidity") or 0) < MIN_LIQ or not is_yes_no(m):
            continue
        ev = event_id(m)
        if ev and per_event.get(ev, 0) >= MAX_PER_EVENT:
            continue
        try:
            p = prices(m)[0]
        except Exception:
            continue
        mp = btc_model(m)
        if mp is not None:
            log = s.setdefault("model_log", [])
            log.append({"t": time.strftime("%F %T"), "q": m["question"][:60], "model": round(mp, 4), "mkt": round(p, 4)})
            del log[:-30]
        f = mp if mp is not None else fair_yes(m, p)
        src = "btc-model" if mp is not None else "bias-guess"
        yes_cost = min(0.99, p + HAIRCUT)
        no_cost = min(0.99, (1 - p) + HAIRCUT)
        if f - yes_cost >= MIN_EDGE:
            side, price, fp = "YES", yes_cost, f
        elif (1 - f) - no_cost >= MIN_EDGE:
            side, price, fp = "NO", no_cost, 1 - f
        else:
            continue
        if price <= 0.01 or price >= 0.99:
            continue
        kelly = max(0.0, (fp - price) / (1 - price))
        eq = equity(s)
        size = min(MAX_FRAC * eq, 0.5 * kelly * eq, s["cash"])
        if size < 0.5:
            continue
        s["cash"] -= size
        s["open"].append({"id": m["id"], "q": m["question"][:80], "side": side,
                          "price": round(price, 3), "cost": round(size, 2),
                          "shares": round(size / price, 3), "ev": ev, "fair": round(fp, 3), "src": src,
                          "opened": time.strftime("%F %T")})
        if ev:
            per_event[ev] = per_event.get(ev, 0) + 1
        tg(f"OPEN {side} ${size:.2f} @ {price:.3f} | fair {fp:.3f} [{src}]\n{m['question'][:80]}")


def commands(s):
    if not (TOKEN and CHAT):
        return
    try:
        r = get(f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={s['offset'] + 1}&timeout=0")
        for u in r.get("result", []):
            s["offset"] = u["update_id"]
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != CHAT:
                continue
            if (msg.get("text") or "").startswith(("/status", "/start")):
                tg(status(s))
    except Exception as e:
        print("telegram poll failed:", e)


def daily_summary(s):
    now = time.gmtime()
    today = time.strftime("%F", now)
    if now.tm_hour >= SUMMARY_HOUR_UTC and s["last_summary"] != today:
        s["last_summary"] = today
        tg("Daily summary\n" + status(s))


def main():
    s = load()
    if not s["dead"]:
        settle(s)
        scan(s)
        s["cash"] = round(s["cash"], 2)
        if equity(s) <= 0.5:
            s["dead"] = True
    if s["dead"] and not s["dead_notified"]:
        s["dead_notified"] = True
        tg("Bot is dead (balance hit zero).")
    commands(s)
    daily_summary(s)
    json.dump(s, open(STATE, "w"), indent=1)
    print(status(s).splitlines()[0])


try:
    main()
except Exception:
    tg("Bot crashed:\n" + traceback.format_exc()[-1500:])
    raise
