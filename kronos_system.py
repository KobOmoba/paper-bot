"""Kronos paper system (see KRONOS_SPEC.md, locked before this code). PAPER ONLY, no real orders.
Layer 1 Kronos forecast (20 paths per asset) -> Layer 2 gates (FTFC, XGBoost, FVG) -> Layer 3 paper execution with hard risk rules.
Books: A Kronos only | B +FTFC | C +XGB | D +FVG | E all gates | F buy-and-hold SPY benchmark.
One run processes every completed bar since the last run (idempotent), then forecasts every asset that has a new completed bar."""
import json, os, sys, time, math, urllib.request, urllib.parse
import numpy as np
import pandas as pd

UNIVERSE = ["BTC-USD", "ETH-USD", "SOL-USD", "SPY", "QQQ", "IWM", "GLD", "TLT", "IEF", "EEM"]
CRYPTO = {"BTC-USD", "ETH-USD", "SOL-USD"}
H, N_PATHS, ENTRY_MIN, XGB_P, STOP = 5, 20, 0.01, 0.60, 0.08
MAXPOS, WEIGHT, CASHMIN, BANK = 5, 0.18, 0.10, 10000.0
FEE_CRYPTO_TAKER, FEE_CRYPTO_MAKER, FEE_EQ = 0.0010, 0.0005, 0.0002
FVG_LOOK, FVG_TTL = 30, 2
CRYPTO_SKIP = 1     # crypto runs mid-day: the current UTC day is already half over, so the first tradable open is one bar later
EQ_OPEN_UTC_HOUR = 13.5   # earliest US open in UTC (EDT); orders for an equity are refused if the run happens after the next open
BOOKS = {"A": set(), "B": {"ftfc"}, "C": {"xgb"}, "D": {"fvg"}, "E": {"ftfc", "xgb", "fvg"}}
STATE_F, FC_F = "kronos_state.json", "kronos_forecasts.json"
TOKEN, CHAT = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


def tg(msg):
    if not (TOKEN and CHAT):
        print("TG:", msg); return
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                               data=urllib.parse.urlencode({"chat_id": CHAT, "text": msg[:3900]}).encode()), timeout=20)
    except Exception as e:
        print("tg fail", e)


def fee(asset, kind):
    if asset in CRYPTO:
        return FEE_CRYPTO_MAKER if kind == "limit" else FEE_CRYPTO_TAKER
    return FEE_EQ


# ---------------------------------------------------------------- data
def fetch(now):
    """Daily OHLCV per asset, only COMPLETED bars: crypto bar of today (UTC) is incomplete; equity bar of today is complete only after 21:00 UTC."""
    import yfinance as yf
    raw = yf.download(UNIVERSE, period="6y", auto_adjust=True, progress=False, group_by="column")
    out = {}
    for a in UNIVERSE:
        d = pd.DataFrame({c.lower(): raw[c][a] for c in ["Open", "High", "Low", "Close", "Volume"]}).dropna()
        d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
        out[a] = complete(a, d, now)
    return out


def complete(a, d, now):
    today = pd.Timestamp(now.strftime("%Y-%m-%d"))
    if a in CRYPTO or now.hour < 21:
        return d[d.index < today]
    return d[d.index <= today]


# ---------------------------------------------------------------- gates
def rsi(c, n=14):
    dlt = c.diff(); up = dlt.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); dn = (-dlt.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def features(d, a):
    c, h, l = d["close"], d["high"], d["low"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    f = pd.DataFrame(index=d.index)
    f["rsi"] = rsi(c); f["atrp"] = atr / c
    f["d20"] = (c - c.ewm(span=20, adjust=False).mean()) / atr; f["d50"] = (c - c.ewm(span=50, adjust=False).mean()) / atr
    f["r5"] = c.pct_change(5); f["r20"] = c.pct_change(20); f["crypto"] = 1.0 if a in CRYPTO else 0.0
    return f   # every value uses data through that bar only


def xgb_prob(dfs, T_by_asset):
    """Pooled XGBoost, rolling 2y window, refit monthly: training rows end at (first day of T's month - 7 days) so 5-bar labels cannot leak. Returns {asset: P(up) or None}."""
    import xgboost as xgb
    out = {}
    cache = {}
    for a, T in T_by_asset.items():
        cut = pd.Timestamp(T.year, T.month, 1) - pd.Timedelta(days=7)
        if cut not in cache:
            X, y = [], []
            for b, d in dfs.items():
                f = features(d, b); lab = (d["close"].shift(-H) / d["close"] - 1 > 0).astype(float)
                m = (f.index <= cut) & (f.index > cut - pd.Timedelta(days=730))
                ff = f[m].join(lab.rename("y")).dropna()
                X.append(ff.drop(columns="y")); y.append(ff["y"])
            X, y = pd.concat(X), pd.concat(y)
            if len(X) < 600 or y.nunique() < 2:
                cache[cut] = None
            else:
                mdl = xgb.XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, random_state=7, verbosity=0)
                mdl.fit(X, y); cache[cut] = mdl
        mdl = cache[cut]
        if mdl is None:
            out[a] = None; continue
        row = features(dfs[a], a).loc[[T]]
        out[a] = None if row.isna().values.any() else round(float(mdl.predict_proba(row)[0, 1]), 4)
    return out


def ftfc(d):
    """Price above the open of the current month, current week and the 3-bar period (open of the bar 2 bars back)."""
    last = d.iloc[-1]; T = d.index[-1]
    mo = d[(d.index.year == T.year) & (d.index.month == T.month)]["open"].iloc[0]
    iso = d.index.isocalendar(); wk = d[(iso.year == T.isocalendar().year) & (iso.week == T.isocalendar().week)]["open"].iloc[0]
    o3 = d["open"].iloc[-3]
    return bool(last["close"] > mo and last["close"] > wk and last["close"] > o3)


def fvg_limit(d):
    """Top edge of the nearest unfilled bullish fair-value gap below the last close (gap: low[i] > high[i-2]). None if there is none."""
    h, l, n = d["high"].values, d["low"].values, len(d)
    last = d["close"].values[-1]; best = None
    for i in range(max(2, n - FVG_LOOK), n):
        if l[i] > h[i - 2]:
            top = l[i]
            if top < last and (i == n - 1 or l[i + 1:].min() > top):
                if best is None or top > best:
                    best = float(top)
    return best


# ---------------------------------------------------------------- Kronos
def make_predictor():
    sys.path.insert(0, "Kronos_repo")
    from model import Kronos, KronosTokenizer, KronosPredictor
    tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base"); mdl = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    pr = KronosPredictor(mdl, tok, max_context=512)

    def run(dfs, assets):
        xs, ys, dl = [], [], []
        for a in assets:
            d = dfs[a].iloc[-512:].copy(); d["amount"] = d["volume"] * d["close"]
            T = d.index[-1]
            fut = pd.date_range(T + pd.Timedelta(days=1), periods=H + CRYPTO_SKIP) if a in CRYPTO else pd.bdate_range(T + pd.Timedelta(days=1), periods=H + CRYPTO_SKIP)
            dl.append(d.reset_index(drop=True)); xs.append(pd.Series(d.index)); ys.append(pd.Series(fut))
        paths = {a: [] for a in assets}
        for _ in range(N_PATHS):     # sample_count=1 per call keeps every path (the library averages when sample_count>1)
            res = pr.predict_batch(df_list=dl, x_timestamp_list=xs, y_timestamp_list=ys, pred_len=H + CRYPTO_SKIP, T=1.0, top_p=0.9, sample_count=1, verbose=False)
            for a, r in zip(assets, res):
                k = CRYPTO_SKIP if a in CRYPTO else 0       # entry bar = first TRADABLE bar; exit = H bars later
                paths[a].append((float(r["open"].iloc[k]), float(r["close"].iloc[k + H - 1])))
        return paths
    return run


def summarize_paths(p):
    r = np.array([c / o - 1 for o, c in p])
    return {"mean": float(r.mean()), "median": float(np.median(r)), "std": float(r.std()), "prob_up": float((r > 0).mean())}


# ---------------------------------------------------------------- state
def new_book():
    return {"cash": BANK, "pos": {}, "pend": {}, "closed": [], "fills": 0, "missed": 0, "no_fvg": 0, "rejected": 0}


def load():
    if os.path.exists(STATE_F):
        s = json.load(open(STATE_F))
    else:
        s = {"books": {k: new_book() for k in BOOKS}, "last_bar": {}, "fc_bar": {}, "last_close": {}, "curve": [], "F": {"cash": BANK, "shares": 0.0, "bought": False, "fee": 0.0},
             "started": time.strftime("%F %T"), "runs": 0, "log": []}
    return s


def late(a, T, now):
    """True if the next tradable open for this asset has already passed when the decision is made (then no order is placed)."""
    if a in CRYPTO:
        return now >= (T + pd.Timedelta(days=1 + CRYPTO_SKIP)).to_pydatetime()      # 00:00 UTC of bar T+2
    nxt = (T + pd.offsets.BDay(1)).to_pydatetime() + pd.Timedelta(hours=EQ_OPEN_UTC_HOUR).to_pytimedelta()
    return now >= nxt


def migrate(s, fcs):
    """One-off: crypto rows/orders made before the timing fix (entry at an open that had already passed) are kept in the log but marked invalid and their unfilled orders cancelled."""
    if s.get("schema") == "v2": return
    cancelled = []
    for r in fcs:
        if r["asset"] in CRYPTO and r.get("timing") != "v2":
            r["timing"] = "v1_crypto_invalid"
    for k, b in s["books"].items():
        for a in [x for x in b["pend"] if x in CRYPTO]:
            cancelled.append((k, a)); del b["pend"][a]
    for r in fcs:
        if r["asset"] not in CRYPTO and r.get("timing") is None: r["timing"] = "v2"; r["late_run"] = False
    s["schema"] = "v2"; s["log"].append({"migration": "v2 crypto timing fix", "cancelled_unfilled_orders": cancelled})


def equity(book, last_close):
    return book["cash"] + sum(p["shares"] * last_close.get(a, p["entry"]) for a, p in book["pos"].items())


def sell(book, a, price, kind, reason, D, last_close):
    p = book["pos"].pop(a)
    proceeds = p["shares"] * price * (1 - fee(a, "market"))
    book["cash"] += proceeds
    book["closed"].append({"asset": a, "entry": p["entry"], "exit": price, "entry_date": p["entry_date"], "exit_date": str(D.date()), "reason": reason,
                           "ret": proceeds / p["cost"] - 1, "kind_in": p["kind"], "pnl": proceeds - p["cost"]})


def process_bar(book, a, D, bar, last_close):
    pos, pend = book["pos"].get(a), book["pend"].get(a)
    if pos: pos["n"] += 1
    if pend: pend["n"] += 1
    if pos and pos.get("exit_next_open"):
        sell(book, a, float(bar["open"]), "market", "stop", D, last_close); pos = None
    if pend and a not in book["pos"] and pend.get("skip", 0) > 0:
        pend["skip"] -= 1                                  # bar already past at decision time: cannot be traded
    elif pend and a not in book["pos"]:
        pend["elig"] = pend.get("elig", 0) + 1
        px = None
        if pend["type"] == "market":
            px = float(bar["open"])
        else:
            lim = pend["limit"]
            if bar["open"] <= lim: px = float(bar["open"])
            elif bar["low"] <= lim: px = float(lim)
        if px is not None:
            eq = equity(book, last_close)
            target = min(WEIGHT * eq, book["cash"] - CASHMIN * eq)
            f = fee(a, pend["type"])
            if len(book["pos"]) >= MAXPOS or target < 50:
                book["rejected"] += 1
            else:
                sh = target / (px * (1 + f)); cost = sh * px * (1 + f)
                book["cash"] -= cost
                book["pos"][a] = {"shares": sh, "entry": px, "cost": cost, "entry_date": str(D.date()), "n": pend["n"], "peak": px, "kind": pend["type"], "exit_next_open": False,
                                  "h": H + (CRYPTO_SKIP if a in CRYPTO else 0)}
                book["fills"] += 1
            del book["pend"][a]; pend = None
        elif pend["elig"] >= (FVG_TTL if pend["type"] == "limit" else 1):
            book["missed"] += 1; del book["pend"][a]
    pos = book["pos"].get(a)
    if pos:
        if pos["n"] >= pos.get("h", H):
            sell(book, a, float(bar["close"]), "market", "time", D, last_close)
        else:
            pos["peak"] = max(pos["peak"], float(bar["close"]))
            if bar["close"] <= pos["peak"] * (1 - STOP): pos["exit_next_open"] = True


def run_once(now, dfs, predict_fn, state=None, forecasts=None, quiet=False):
    s = state or load(); fcs = forecasts if forecasts is not None else (json.load(open(FC_F)) if os.path.exists(FC_F) else [])
    migrate(s, fcs)
    first = not s["last_bar"]
    if first:
        for a in UNIVERSE:
            s["last_bar"][a] = str(dfs[a].index[-1].date()); s["last_close"][a] = float(dfs[a]["close"].iloc[-1])     # start fresh: no trades for past bars
    # 1) process new completed bars in date order
    events = []
    for a in UNIVERSE:
        lb = pd.Timestamp(s["last_bar"][a])
        for D, bar in dfs[a][dfs[a].index > lb].iterrows(): events.append((D, UNIVERSE.index(a), a, bar))
    events.sort(key=lambda e: (e[0], e[1]))
    cur_date = None
    def mark(D):
        s["curve"].append({"date": str(D.date()), **{k: round(equity(b, s["last_close"]), 2) for k, b in s["books"].items()}, "F": round(s["F"]["cash"] + s["F"]["shares"] * s["last_close"].get("SPY", 0), 2)})
    for D, _, a, bar in events:
        if cur_date is not None and D != cur_date: mark(cur_date)
        cur_date = D
        if a == "SPY" and not s["F"]["bought"]:
            f_ = FEE_EQ; s["F"]["shares"] = BANK / (float(bar["open"]) * (1 + f_)); s["F"]["cash"] = 0.0; s["F"]["bought"] = True; s["F"]["entry"] = float(bar["open"]); s["F"]["entry_date"] = str(D.date())
        for k, b in s["books"].items(): process_bar(b, a, D, bar, s["last_close"])
        s["last_close"][a] = float(bar["close"]); s["last_bar"][a] = str(D.date())
    if cur_date is not None: mark(cur_date)
    # 2) score matured forecasts
    for r in fcs:
        if r.get("realized") is None:
            d = dfs[r["asset"]]; T = pd.Timestamp(r["date"]); fut = d[d.index > T]
            sk = CRYPTO_SKIP if (a_ := r["asset"]) in CRYPTO else 0
            if r.get("timing") != "v2":
                continue                                   # pre-fix crypto rows are excluded from scoring (see migrate)
            if len(fut) >= H + sk:
                o1, c5 = float(fut["open"].iloc[sk]), float(fut["close"].iloc[sk + H - 1]); cT = float(d.loc[T, "close"])
                r["realized"] = c5 / o1 - 1; r["realized_from_close"] = c5 / cT - 1
                r["kronos_hit"] = (r["mean"] > 0) == (r["realized"] > 0)
                r["xgb_hit"] = None if r.get("xgb_p") is None else ((r["xgb_p"] > 0.5) == (r["realized_from_close"] > 0))
    # 3) forecast every asset with a new completed bar
    todo = [a for a in UNIVERSE if s["fc_bar"].get(a) != str(dfs[a].index[-1].date())]
    new_fc = []
    paths = xp = None
    if todo:
        try:
            paths = predict_fn(dfs, todo)
            T_by = {a: dfs[a].index[-1] for a in todo}
            xp = xgb_prob(dfs, T_by)
        except Exception as e:      # never trade on a failed forecast; bars already processed are still saved
            s["log"].append({"t": now.strftime("%F %T"), "error": repr(e)[:300]}); tg("KRONOS forecast step FAILED: " + repr(e)[:300]); todo = []
        for a in todo:
            sm = summarize_paths(paths[a]); T = T_by[a]
            row = {"date": str(T.date()), "asset": a, **{k: round(v, 5) for k, v in sm.items()}, "signal": sm["mean"] >= ENTRY_MIN, "ftfc": ftfc(dfs[a]), "xgb_p": xp[a],
                   "fvg_limit": fvg_limit(dfs[a]), "last_close": float(dfs[a]["close"].iloc[-1]), "realized": None, "created": now.strftime("%F %T"), "timing": "v2",
                   "late_run": late(a, T, now)}
            new_fc.append(row); s["fc_bar"][a] = str(T.date())
        fcs.extend(new_fc)
        # 4) create orders for each book
        for k, gates in BOOKS.items():
            b = s["books"][k]
            cands = []
            for r in new_fc:
                a = r["asset"]
                if not r["signal"] or a in b["pos"] or a in b["pend"] or r.get("late_run"): continue
                if "ftfc" in gates and not r["ftfc"]: continue
                if "xgb" in gates and not (r["xgb_p"] is not None and r["xgb_p"] > XGB_P): continue
                cands.append(r)
            cands.sort(key=lambda r: -r["mean"])
            room = MAXPOS - len(b["pos"]) - len(b["pend"])
            for r in cands[:max(room, 0)]:
                a = r["asset"]
                if "fvg" in gates:
                    if r["fvg_limit"] is None: b["no_fvg"] += 1; continue
                    b["pend"][a] = {"type": "limit", "limit": r["fvg_limit"], "n": 0, "signal_date": r["date"], "skip": CRYPTO_SKIP if a in CRYPTO else 0}
                else:
                    b["pend"][a] = {"type": "market", "n": 0, "signal_date": r["date"], "skip": CRYPTO_SKIP if a in CRYPTO else 0}
    s["runs"] += 1
    s["log"] = (s["log"] + [{"t": now.strftime("%F %T"), "events": len(events), "forecasts": len(new_fc), "last_bars": {a: str(dfs[a].index[-1].date()) for a in UNIVERSE}}])[-60:]
    if not quiet: json.dump(s, open(STATE_F, "w")); json.dump(fcs, open(FC_F, "w"))
    return s, fcs, new_fc


def summary(s, new_fc):
    lines = [f"KRONOS paper | runs {s['runs']} | started {s['started'][:10]}"]
    c = s["curve"][-1] if s["curve"] else {}
    for k in "ABCDEF":
        if k in c:
            extra = ""
            if k in s["books"]:
                b = s["books"][k]; extra = f" pos {len(b['pos'])} pend {len(b['pend'])} closed {len(b['closed'])} fills {b['fills']} missed {b['missed']}"
            lines.append(f"{k}: ${c[k]:,.0f}{extra}")
    for r in new_fc:
        lines.append(f"{r['asset']:8s} fc {r['mean']*100:+.2f}% up {r['prob_up']*100:.0f}% ftfc {'Y' if r['ftfc'] else 'n'} xgb {r['xgb_p']} fvg {r['fvg_limit']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import datetime
    now = datetime.datetime.utcnow()
    dfs = fetch(now)
    s, fcs, new = run_once(now, dfs, make_predictor())
    tg(summary(s, new))
