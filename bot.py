"""Paper-trading bot for Polymarket. FAKE money only. No keys, no wallet.
Each run: settle finished markets, scan active ones, open simulated trades.
State lives in state.json (committed back to the repo by the workflow)."""
import json, os, time, urllib.request

LIST = ("https://gamma-api.polymarket.com/markets?active=true&closed=false"
        "&limit=200&order=volume24hr&ascending=false")
ONE = "https://gamma-api.polymarket.com/markets/"
STATE = "state.json"
START = 50.0       # fake starting bankroll
MAX_FRAC = 0.06    # max 6% of equity per trade
MIN_EDGE = 0.02    # minimum gap between your fair value and market price
MIN_LIQ = 5000     # skip thin markets


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-bot"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def prices(m):
    p = m.get("outcomePrices")
    if isinstance(p, str):
        p = json.loads(p)
    return [float(x) for x in p]


def fair_yes(m, p):
    """THE ONLY PART THAT MATTERS. Your estimate of the true YES probability.
    This placeholder assumes the favorite-longshot bias (cheap contracts are
    overpriced, near-certain ones underpriced). It is UNPROVEN. Replace it
    with a better idea and let the paper results judge it."""
    if p < 0.08:
        return p * 0.6
    if p > 0.92:
        return min(0.99, p + 0.03)
    return p  # no opinion


def load():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"cash": START, "open": [], "closed": [], "dead": False}


def equity(s):
    return s["cash"] + sum(t["cost"] for t in s["open"])


def settle(s):
    keep = []
    for t in s["open"]:
        try:
            m = get(ONE + str(t["id"]))
            pr = prices(m)
        except Exception:
            keep.append(t)
            continue
        if m.get("closed") and max(pr) >= 0.99:
            won = pr[0 if t["side"] == "YES" else 1] >= 0.99
            payout = t["shares"] if won else 0.0
            s["cash"] += payout
            t.update(payout=round(payout, 2), won=won, settled=time.strftime("%F %T"))
            s["closed"].append(t)
        else:
            keep.append(t)
    s["open"] = keep


def scan(s):
    held = {t["id"] for t in s["open"]}
    for m in get(LIST):
        if m["id"] in held or float(m.get("liquidity") or 0) < MIN_LIQ:
            continue
        try:
            p = prices(m)[0]
        except Exception:
            continue
        f = fair_yes(m, p)
        edge_yes, edge_no = f - p, p - f
        if edge_yes >= MIN_EDGE:
            side, price, fp = "YES", p, f
        elif edge_no >= MIN_EDGE:
            side, price, fp = "NO", 1 - p, 1 - f
        else:
            continue
        if price <= 0.01 or price >= 0.99:
            continue
        kelly = max(0.0, (fp - price) / (1 - price))
        size = min(MAX_FRAC * equity(s), 0.5 * kelly * equity(s), s["cash"])
        if size < 0.5:
            continue
        s["cash"] -= size
        s["open"].append({"id": m["id"], "q": m["question"][:80], "side": side,
                          "price": round(price, 3), "cost": round(size, 2),
                          "shares": round(size / price, 3),
                          "opened": time.strftime("%F %T")})


def main():
    s = load()
    if s.get("dead"):
        print("Bot is dead (balance hit zero).")
        return
    settle(s)
    scan(s)
    s["cash"] = round(s["cash"], 2)
    if equity(s) <= 0.5:
        s["dead"] = True
    json.dump(s, open(STATE, "w"), indent=1)
    wins = sum(1 for t in s["closed"] if t["won"])
    print(f"equity ${equity(s):.2f} | cash ${s['cash']:.2f} | open {len(s['open'])} "
          f"| settled {len(s['closed'])} (wins {wins})")


main()

