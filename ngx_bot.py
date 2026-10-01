"""NGX paper-trading + watchlist bot. FAKE naira only. No broker, no keys, no real orders.
Once per trading day (after the close) it:
  1. downloads NGX prices (official public feed, ~30 min delayed),
  2. stores a growing price history,
  3. runs a simple, transparent breakout strategy on paper,
  4. sends a digest to Telegram (watchlist, movers, paper portfolio).
State lives in ngx_state.json (committed back by the workflow).
Educational use only. NGX owns its market data: do not redistribute it."""
import json, os, re, sys, time, traceback, urllib.parse, urllib.request
from collections import Counter

URL = ("https://doclib.ngxgroup.com/REST/api/statistics/equities/"
       "?market=&sector=&orderby=&pageSize=300&pageNo=0")
STATE = "ngx_state.json"

START = 1_000_000.0      # fake starting cash (naira)
LOOKBACK = 20            # breakout window (sessions)
EXIT_LOW = 10            # trend exit: close below the lowest close of last 10 sessions
VOL_MULT = 1.5           # breakout day volume must beat 1.5x the recent average
MIN_AVG_VALUE = 10_000_000   # skip stocks trading under N10m/day on average (hard to exit)
MAX_POS = 5              # max simultaneous paper positions
POS_FRAC = 0.10          # 10% of equity per position
STOP = 0.08              # exit if close falls 8% below entry
TARGET = 0.20            # exit if close rises 20% above entry
MAX_HOLD = 60            # exit after 60 sessions
SLIP = 0.005             # pessimistic fill: 0.5% worse than the opening price
FEE = 0.01               # ~1% per side for brokerage + levies (approximation; check your broker)
SKIP_LIMIT_UP = 9.0      # NGX caps daily moves at 10%; a +9% day is probably unbuyable, skip it
HIST_KEEP = 120

# Edit this list. Tickers not found in the feed are reported, not fatal.
WATCH = ["SEPLAT", "ZENITHBANK", "GTCO", "MTNN", "DANGCEM", "AIRTELAFRI",
         "ACCESSCORP", "UBA", "FIRSTHOLDCO", "BUACEMENT", "STANBIC", "NESTLE"]

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ngx-paper-bot; educational)"}

KEYS = {
    "sym": ["symbol", "ticker"],
    "name": ["company", "companyname", "securityname", "name"],
    "open": ["openingprice", "openprice", "open"],
    "close": ["closeprice", "closingprice", "close", "lastprice", "price"],
    "prev": ["prevclosingprice", "previousclosingprice", "previousclose", "prevclose"],
    "vol": ["volume", "totalvolume", "vol"],
    "date": ["tradedate", "tradingdate", "date"],
}


# ---------- helpers ----------
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


def norm(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v == v else None
    except Exception:
        return None


def to_date(v):
    v = str(v or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return "-".join(m.groups())
    m = re.match(r"(\d{2})[/-](\d{2})[/-](\d{4})", v)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    return None


def find_rows(j):
    if isinstance(j, list) and j and isinstance(j[0], dict):
        return j
    if isinstance(j, dict):
        for v in j.values():
            r = find_rows(v)
            if r:
                return r
    return []


def pick(rec, field):
    for n in KEYS[field]:
        if n in rec and rec[n] not in (None, ""):
            return rec[n]
    return None


# ---------- data ----------
def fetch():
    last = None
    for i in range(3):
        try:
            raw = json.load(urllib.request.urlopen(
                urllib.request.Request(URL, headers=UA), timeout=40))
            break
        except Exception as e:
            last = e
            time.sleep(5 * (i + 1))
    else:
        raise RuntimeError(f"NGX feed unreachable: {last}")
    rows = find_rows(raw)
    if not rows:
        raise RuntimeError("NGX feed returned no rows. Top-level keys: "
                           + str(list(raw)[:10] if isinstance(raw, dict) else type(raw)))
    q, dates = {}, Counter()
    for r in rows:
        rec = {norm(k): v for k, v in r.items()}
        sym, close = pick(rec, "sym"), num(pick(rec, "close"))
        if not sym or not close or close <= 0:
            continue
        d = to_date(pick(rec, "date"))
        if d:
            dates[d] += 1
        q[str(sym).strip().upper()] = {
            "name": str(pick(rec, "name") or sym)[:40],
            "open": num(pick(rec, "open")) or 0.0,
            "close": close,
            "prev": num(pick(rec, "prev")) or 0.0,
            "vol": num(pick(rec, "vol")) or 0.0,
        }
    if not q:
        raise RuntimeError("Could not parse prices. Field names seen: "
                           + str(sorted({norm(k) for k in rows[0]})[:25]))
    date = dates.most_common(1)[0][0] if dates else time.strftime("%F")
    return q, date


# ---------- state ----------
def load():
    if os.path.exists(STATE):
        s = json.load(open(STATE))
    else:
        s = {}
    s.setdefault("cash", START)
    for k in ("pos", "pending", "closed"):
        s.setdefault(k, [])
    s.setdefault("hist", {})
    s.setdefault("last_date", "")
    return s


def equity(s):
    return s["cash"] + sum(p["shares"] * p["mark"] for p in s["pos"])


def pct(r):
    base = r["prev"] or 0
    return (r["close"] - base) / base * 100 if base else 0.0


def n(x):
    return f"{x:,.2f}"


# ---------- strategy ----------
def fill_pending(s, q):
    keep = []
    for o in s["pending"]:
        r = q.get(o["sym"])
        o["tries"] = o.get("tries", 0) + 1
        if not r:
            if o["tries"] < 3:
                keep.append(o)
            continue
        px = r["open"] or r["close"]
        if o["side"] == "BUY":
            price = px * (1 + SLIP)
            if price > o["ref"] * 1.10:
                tg(f"SKIPPED BUY {o['sym']}: opened too far above signal price")
                continue
            budget = min(POS_FRAC * equity(s), s["cash"])
            shares = int(budget / (price * (1 + FEE)))
            if shares < 1:
                continue
            s["cash"] -= shares * price * (1 + FEE)
            s["pos"].append({"sym": o["sym"], "name": r["name"], "entry": round(price, 2),
                             "shares": shares, "mark": r["close"], "days": 0,
                             "opened": o["date"]})
            tg(f"PAPER BUY {o['sym']} {shares} sh @ N{n(price)} (fees incl. N{n(shares*price*FEE)})")
        else:
            p = next((x for x in s["pos"] if x["sym"] == o["sym"]), None)
            if not p:
                continue
            price = px * (1 - SLIP)
            proceeds = p["shares"] * price * (1 - FEE)
            cost = p["shares"] * p["entry"] * (1 + FEE)
            s["cash"] += proceeds
            s["pos"].remove(p)
            p.update(exit=round(price, 2), pnl=round(proceeds - cost, 2),
                     reason=o["reason"], closed=o["date"])
            s["closed"].append(p)
            tg(f"PAPER SELL {o['sym']} ({o['reason']}) @ N{n(price)} | P&L N{n(p['pnl'])}")
    s["pending"] = keep


def update_history(s, q, date):
    for sym, r in q.items():
        h = s["hist"].setdefault(sym, [])
        if not h or h[-1][0] != date:
            h.append([date, r["close"], r["vol"]])
            del h[:-HIST_KEEP]


def signals(s, q, date):
    held = {p["sym"] for p in s["pos"]}
    queued = {o["sym"] for o in s["pending"]}
    # exits
    for p in s["pos"]:
        r = q.get(p["sym"])
        if r:
            p["mark"] = r["close"]
        p["days"] += 1
        if p["sym"] in queued:
            continue
        h = s["hist"].get(p["sym"], [])
        why = None
        if p["mark"] <= p["entry"] * (1 - STOP):
            why = "stop"
        elif p["mark"] >= p["entry"] * (1 + TARGET):
            why = "target"
        elif len(h) > EXIT_LOW and p["mark"] < min(x[1] for x in h[-EXIT_LOW - 1:-1]):
            why = "trend"
        elif p["days"] >= MAX_HOLD:
            why = "time"
        if why:
            s["pending"].append({"side": "SELL", "sym": p["sym"], "reason": why, "date": date})
            tg(f"SELL SIGNAL {p['sym']} ({why}) - fills at next open")
    # entries
    slots = MAX_POS - len(s["pos"]) - sum(1 for o in s["pending"] if o["side"] == "BUY")
    cands = []
    for sym, r in q.items():
        h = s["hist"].get(sym, [])
        if sym in held or sym in queued or len(h) < LOOKBACK + 1:
            continue
        prior = h[-LOOKBACK - 1:-1]
        avg_vol = sum(x[2] for x in prior) / LOOKBACK
        avg_val = sum(x[1] * x[2] for x in prior) / LOOKBACK
        if (r["close"] > max(x[1] for x in prior) and avg_vol > 0
                and r["vol"] > VOL_MULT * avg_vol and avg_val >= MIN_AVG_VALUE
                and pct(r) < SKIP_LIMIT_UP):
            cands.append((r["vol"] / avg_vol, sym, r))
    for ratio, sym, r in sorted(cands, reverse=True)[:max(slots, 0)]:
        s["pending"].append({"side": "BUY", "sym": sym, "ref": r["close"], "date": date})
        tg(f"BUY SIGNAL {sym} @ N{n(r['close'])} (20d breakout, volume {ratio:.1f}x) - fills at next open")


def digest(s, q, date):
    liquid = [(pct(r), sym, r) for sym, r in q.items() if r["close"] * r["vol"] >= 5_000_000 and r["prev"]]
    liquid.sort()
    lines = [f"NGX close {date}"]
    if liquid:
        lines.append("Top gainers: " + ", ".join(f"{sy} {p:+.1f}%" for p, sy, _ in liquid[::-1][:5]))
        lines.append("Top losers: " + ", ".join(f"{sy} {p:+.1f}%" for p, sy, _ in liquid[:5]))
    lines.append("\nWatchlist")
    missing = []
    for sym in WATCH:
        r = q.get(sym)
        if r:
            lines.append(f"{sym} N{n(r['close'])} ({pct(r):+.1f}%)")
        else:
            missing.append(sym)
    if missing:
        lines.append("Not in feed: " + ", ".join(missing))
    eq = equity(s)
    lines.append(f"\nPaper portfolio: N{n(eq)} (start N{n(START)}, {(eq/START-1)*100:+.1f}%) | cash N{n(s['cash'])}")
    for p in s["pos"]:
        lines.append(f"- {p['sym']} {p['shares']} sh | {(p['mark']/p['entry']-1)*100:+.1f}%")
    wins = sum(1 for c in s["closed"] if c["pnl"] > 0)
    lines.append(f"Closed trades: {len(s['closed'])} (wins {wins})")
    have = max((len(h) for h in s["hist"].values()), default=0)
    if have < LOOKBACK + 1:
        lines.append(f"History: {have}/{LOOKBACK + 1} sessions - strategy starts trading after this.")
    tg("\n".join(lines))


def process_day(s, q, date):
    fill_pending(s, q)
    update_history(s, q, date)
    signals(s, q, date)
    for p in s["pos"]:
        r = q.get(p["sym"])
        if r:
            p["mark"] = r["close"]
    s["last_date"] = date
    s["cash"] = round(s["cash"], 2)
    digest(s, q, date)


def run():
    s = load()
    q, date = fetch()
    if "--debug" in sys.argv:
        print(json.dumps(next(iter(q.items())), indent=1), date, len(q))
    if date <= s["last_date"]:
        print(f"No new session (latest {date}).")
        return
    process_day(s, q, date)
    json.dump(s, open(STATE, "w"), indent=1)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        tg("NGX bot problem:\n" + traceback.format_exc()[-1200:])
        raise
