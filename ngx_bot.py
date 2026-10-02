"""NGX paper-trading bot, multi-strategy. FAKE naira only. No broker, no keys, no real orders.
Daily (after the close): fetch NGX prices -> update history -> three strategies trade on paper,
each with its own wallet -> Telegram digest. `--backtest` replays saved history (ngx_history.json).
Educational use only. NGX owns its market data: do not redistribute it."""
import json, os, re, sys, time, traceback, urllib.parse, urllib.request
from collections import Counter

URL = ("https://doclib.ngxgroup.com/REST/api/statistics/equities/"
       "?market=&sector=&orderby=&pageSize=300&pageNo=0")
STATE, HISTFILE = "ngx_state.json", "ngx_history.json"

START = 1_000_000.0       # fake naira per strategy wallet
MIN_AVG_VALUE = 10_000_000  # skip stocks trading < N10m/day on average (hard to exit)
MAX_POS = 5               # positions per strategy
POS_FRAC = 0.10           # 10% of that wallet's equity per position
SLIP = 0.005              # pessimistic fill: 0.5% worse than the quoted price
FEE = 0.01                # ~1% per side for brokerage + levies (approximation)
SKIP_LIMIT_UP = 9.0       # NGX caps daily moves at 10%; a +9% day is probably unbuyable
HIST_KEEP = 120
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


def norm(k): return re.sub(r"[^a-z0-9]", "", str(k).lower())
def n(x): return f"{x:,.2f}"
def avg(xs): return sum(xs) / len(xs) if xs else 0.0


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
    for k in KEYS[field]:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


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
        raise RuntimeError("NGX feed returned no rows")
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
            "open": num(pick(rec, "open")) or 0.0, "close": close,
            "prev": num(pick(rec, "prev")) or 0.0, "vol": num(pick(rec, "vol")) or 0.0}
    if not q:
        raise RuntimeError("Could not parse prices. Field names: " + str(sorted({norm(k) for k in rows[0]})[:25]))
    return q, (dates.most_common(1)[0][0] if dates else time.strftime("%F"))


def pct(r):
    base = r["prev"] or 0
    return (r["close"] - base) / base * 100 if base else 0.0


# ---------- strategies: entry(h) -> bool, exit(p, h) -> reason or None ----------
# h = history rows [date, close, volume, pct], newest last, including today.
def liquid(h):
    w = h[-21:-1]
    return len(w) >= 20 and avg([x[1] * x[2] for x in w]) >= MIN_AVG_VALUE


def e_breakout(h):
    if len(h) < 21 or not liquid(h):
        return False
    prior = h[-21:-1]
    av = avg([x[2] for x in prior])
    return h[-1][1] > max(x[1] for x in prior) and av > 0 and h[-1][2] > 1.5 * av


def x_breakout(p, h):
    c = h[-1][1]
    if c <= p["entry"] * 0.92: return "stop"
    if c >= p["entry"] * 1.20: return "target"
    if len(h) > 11 and c < min(x[1] for x in h[-11:-1]): return "trend"
    if p["days"] >= 60: return "time"


def e_dip(h):
    if len(h) < 51 or not liquid(h):
        return False
    c = h[-1][1]
    return c > avg([x[1] for x in h[-50:]]) and c <= 0.92 * max(x[1] for x in h[-11:-1])


def x_dip(p, h):
    c = h[-1][1]
    if c <= p["entry"] * 0.94: return "stop"
    if c >= p["entry"] * 1.06: return "target"
    if p["days"] >= 15: return "time"


def e_trend(h):
    if len(h) < 51 or not liquid(h):
        return False
    c = [x[1] for x in h]
    m20, m50 = avg(c[-20:]), avg(c[-50:])
    p20, p50 = avg(c[-21:-1]), avg(c[-51:-1])
    return m20 > m50 and p20 <= p50 and c[-1] > m50


def x_trend(p, h):
    c = [x[1] for x in h]
    if c[-1] <= p["entry"] * 0.90: return "stop"
    if len(c) >= 20 and c[-1] < avg(c[-20:]): return "trend"
    if p["days"] >= 120: return "time"


def e_regime(h, c, sym):
    """Breakout, but only when most liquid stocks are above their 50-day average."""
    return c["breadth"] >= 0.55 and e_breakout(h)


def e_mom(h, c, sym):
    """Relative-strength momentum: top-10% 60-day performers, in an uptrend, in a healthy market."""
    if len(h) < 61 or not liquid(h) or c["breadth"] < 0.5:
        return False
    cl = [x[1] for x in h]
    return c["rank"].get(sym, 0) >= 0.9 and cl[-1] > avg(cl[-50:])


def x_mom(p, h, c):
    if h[-1][1] <= p["entry"] * 0.88: return "stop"
    if c["rank"].get(p["sym"], 0) < 0.6: return "rank"
    if p["days"] >= 90: return "time"


def e_momo_regime(h, c, sym):
    """Momentum, but only in a clearly healthy market (>=60% of liquid stocks in uptrend)."""
    return c["breadth"] >= 0.6 and e_mom(h, c, sym)


def x_momo_regime(p, h, c):
    if c["breadth"] < 0.4: return "cash"       # market weak: step aside
    return x_mom(p, h, c)


STRATS = {
    "breakout": (lambda h, c, s: e_breakout(h), lambda p, h, c: x_breakout(p, h)),
    "dip": (lambda h, c, s: e_dip(h), lambda p, h, c: x_dip(p, h)),
    "trend": (lambda h, c, s: e_trend(h), lambda p, h, c: x_trend(p, h)),
    "regime_breakout": (e_regime, lambda p, h, c: x_breakout(p, h)),
    "momentum": (e_mom, x_mom),
    "momo_regime": (e_momo_regime, x_momo_regime),
}


def market_ctx(hist):
    """Market-wide context: breadth (share of liquid stocks above 50d avg) and 60-day strength ranks."""
    up = tot = 0
    rets = {}
    for sym, h in hist.items():
        if len(h) >= 51 and liquid(h):
            tot += 1
            up += h[-1][1] > avg([x[1] for x in h[-50:]])
            if len(h) >= 61 and h[-61][1] > 0:
                rets[sym] = h[-1][1] / h[-61][1]
    order = sorted(rets, key=rets.get)
    rank = {sym: i / max(len(order) - 1, 1) for i, sym in enumerate(order)}
    return {"breadth": up / tot if tot else 0.0, "rank": rank}


# ---------- engine ----------
def new_acct(): return {"cash": START, "pos": [], "pending": [], "closed": []}
def equity(a): return a["cash"] + sum(p["shares"] * p["mark"] for p in a["pos"])


def fill_pending(a, q, name, say):
    keep = []
    for o in a["pending"]:
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
                continue
            shares = int(min(POS_FRAC * equity(a), a["cash"]) / (price * (1 + FEE)))
            if shares < 1:
                continue
            a["cash"] -= shares * price * (1 + FEE)
            a["pos"].append({"sym": o["sym"], "entry": round(price, 4), "shares": shares,
                             "mark": r["close"], "days": 0, "opened": o["date"]})
            say(f"[{name}] PAPER BUY {o['sym']} {shares} sh @ N{n(price)}")
        else:
            p = next((x for x in a["pos"] if x["sym"] == o["sym"]), None)
            if not p:
                continue
            price = px * (1 - SLIP)
            proceeds = p["shares"] * price * (1 - FEE)
            cost = p["shares"] * p["entry"] * (1 + FEE)
            a["cash"] += proceeds
            a["pos"].remove(p)
            p.update(exit=round(price, 4), pnl=round(proceeds - cost, 2), cost=round(cost, 2),
                     reason=o["reason"], closed=o["date"])
            a["closed"].append(p)
            say(f"[{name}] PAPER SELL {o['sym']} ({o['reason']}) P&L N{n(p['pnl'])} ({p['pnl']/cost*100:+.1f}%)")
    a["pending"] = keep


def run_signals(a, name, q, hist, date, say, ctx):
    entry, exit_ = STRATS[name]
    held = {p["sym"] for p in a["pos"]}
    queued = {o["sym"] for o in a["pending"]}
    for p in a["pos"]:
        r = q.get(p["sym"])
        if r:
            p["mark"] = r["close"]
        p["days"] += 1
        h = hist.get(p["sym"], [])
        if p["sym"] in queued or not r or not h:
            continue
        why = exit_(p, h, ctx)
        if why:
            a["pending"].append({"side": "SELL", "sym": p["sym"], "reason": why, "date": date})
            say(f"[{name}] SELL SIGNAL {p['sym']} ({why})")
    slots = MAX_POS - len(a["pos"]) - sum(1 for o in a["pending"] if o["side"] == "BUY")
    if slots <= 0:
        return
    cands = []
    for sym, r in q.items():
        h = hist.get(sym, [])
        if sym in held or sym in queued or not h or h[-1][0] != date:
            continue
        if pct(r) < SKIP_LIMIT_UP and entry(h, ctx, sym):
            ratio = h[-1][2] / max(avg([x[2] for x in h[-21:-1]]), 1)
            cands.append((ratio, sym, r))
    for ratio, sym, r in sorted(cands, reverse=True)[:slots]:
        a["pending"].append({"side": "BUY", "sym": sym, "ref": r["close"], "date": date})
        say(f"[{name}] BUY SIGNAL {sym} @ N{n(r['close'])} - fills at next open")


def update_history(s, q, date):
    for sym, r in q.items():
        h = s["hist"].setdefault(sym, [])
        if not h or h[-1][0] < date:
            h.append([date, r["close"], r["vol"], round(pct(r), 2)])
            del h[:-HIST_KEEP]


def process_day(s, q, date, say=tg):
    update_history(s, q, date)
    ctx = market_ctx(s["hist"])
    for name, a in s["accts"].items():
        fill_pending(a, q, name, say)
        run_signals(a, name, q, s["hist"], date, say, ctx)
        for p in a["pos"]:
            if p["sym"] in q:
                p["mark"] = q[p["sym"]]["close"]
    s["last_date"] = date


# ---------- state / digest ----------
def load():
    s = json.load(open(STATE)) if os.path.exists(STATE) else {}
    s.setdefault("hist", {})
    s.setdefault("last_date", "")
    s.setdefault("accts", {})
    for k in STRATS:
        s["accts"].setdefault(k, new_acct())
    for old in ("cash", "pos", "pending", "closed"):
        s.pop(old, None)
    if os.path.exists(HISTFILE) and not s.get("seeded"):
        full = json.load(open(HISTFILE))
        for sym, rows in full.items():
            extra = [r for r in s["hist"].get(sym, []) if rows and r[0] > rows[-1][0]]
            s["hist"][sym] = (rows + extra)[-HIST_KEEP:]
        s["seeded"] = True
    return s


def digest(s, q, date):
    liquid_now = sorted((pct(r), sym) for sym, r in q.items() if r["close"] * r["vol"] >= 5_000_000 and r["prev"])
    L = [f"NGX close {date}"]
    if liquid_now:
        L.append("Gainers: " + ", ".join(f"{sy} {p:+.1f}%" for p, sy in liquid_now[::-1][:5]))
        L.append("Losers: " + ", ".join(f"{sy} {p:+.1f}%" for p, sy in liquid_now[:5]))
    L.append("\nWatchlist")
    miss = []
    for sym in WATCH:
        r = q.get(sym)
        L.append(f"{sym} N{n(r['close'])} ({pct(r):+.1f}%)") if r else miss.append(sym)
    if miss:
        L.append("Not in feed: " + ", ".join(miss))
    L.append("\nPaper strategies (each starts N1,000,000)")
    for name, a in s["accts"].items():
        w = sum(1 for c in a["closed"] if c["pnl"] > 0)
        L.append(f"{name}: N{n(equity(a))} ({(equity(a)/START-1)*100:+.1f}%) | open {len(a['pos'])} | closed {len(a['closed'])} (wins {w})")
        for p in a["pos"]:
            L.append(f"   {p['sym']} {(p['mark']/p['entry']-1)*100:+.1f}%")
    tg("\n".join(L))


# ---------- backtest ----------
def metrics(a, curve, lo, hi, hist):
    tr = [t for t in a["closed"] if lo <= t["closed"] <= hi]
    rets = [t["pnl"] / t["cost"] * 100 for t in tr if t.get("cost")]
    vals = [v for d, v in curve if lo <= d <= hi]
    peak, mdd = (vals[0] if vals else START), 0.0
    for v in vals:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"trades": len(tr), "win_pct": round(100 * sum(1 for r in rets if r > 0) / len(rets), 1) if rets else None,
            "avg_trade_pct": round(avg(rets), 2) if rets else None,
            "return_pct": round((vals[-1] / vals[0] - 1) * 100, 1) if len(vals) > 1 else None,
            "max_drawdown_pct": round(mdd * 100, 1)}


def benchmark(hist, lo, hi):
    out = []
    for rows in hist.values():
        w = [r for r in rows if lo <= r[0] <= hi]
        if len(w) > 20 and avg([r[1] * r[2] for r in w]) >= MIN_AVG_VALUE and w[0][1] > 0:
            out.append(w[-1][1] / w[0][1] - 1)
    return round(avg(out) * 100, 1) if out else None


def backtest(split=0.7, histfile=None, outfile="backtest_report.json", label="NGX"):
    full = json.load(open(histfile or HISTFILE))
    by_date = {}
    for sym, rows in full.items():
        for r in rows:
            by_date.setdefault(r[0], {})[sym] = r
    dates = sorted(by_date)
    s = {"hist": {}, "last_date": "", "accts": {k: new_acct() for k in STRATS}}
    curves = {k: [] for k in STRATS}
    for d in dates:
        q = {}
        for sym, r in by_date[d].items():
            pv = r[3] if len(r) > 3 and r[3] is not None else 0.0
            q[sym] = {"open": 0.0, "close": r[1], "vol": r[2], "prev": r[1] / (1 + pv / 100) if pv > -99 else r[1]}
        process_day(s, q, d, say=lambda m: None)
        for k, a in s["accts"].items():
            curves[k].append((d, equity(a)))
    k = int(len(dates) * split)
    periods = {"build": (dates[0], dates[k]), "test": (dates[k + 1], dates[-1])}
    rep = {"dates": [dates[0], dates[-1]], "sessions": len(dates), "stocks": len(full), "periods": periods,
           "benchmark_liquid_buyhold_pct": {p: benchmark(full, *r) for p, r in periods.items()}, "strategies": {}}
    for name, a in s["accts"].items():
        rep["strategies"][name] = {p: metrics(a, curves[name], *r, full) for p, r in periods.items()}
    json.dump(rep, open(outfile, "w"), indent=1)
    L = [f"{label} backtest {dates[0]} to {dates[-1]} ({len(dates)} sessions, {len(full)} stocks)",
         f"Build period to {dates[k]}, then untouched TEST period after.",
         "Benchmark (buy & hold liquid stocks): " + ", ".join(f"{p} {v}%" for p, v in rep["benchmark_liquid_buyhold_pct"].items())]
    for name, per in rep["strategies"].items():
        for p in ("build", "test"):
            m = per[p]
            L.append(f"{name}/{p}: {m['trades']} trades, win {m['win_pct']}%, avg {m['avg_trade_pct']}%/trade, "
                     f"return {m['return_pct']}%, worst dip {m['max_drawdown_pct']}%")
    tg("\n".join(L))


def run():
    s = load()
    q, date = fetch()
    if "--debug" in sys.argv:
        print(json.dumps(next(iter(q.items())), indent=1), date, len(q))
    if date > s["last_date"]:
        process_day(s, q, date)
        digest(s, q, date)
    else:
        print(f"No new session (latest {date}).")
    json.dump(s, open(STATE, "w"), indent=1)


if __name__ == "__main__":
    try:
        backtest() if "--backtest" in sys.argv else run()
    except Exception:
        tg("NGX bot problem:\n" + traceback.format_exc()[-1200:])
        raise
