"""Crypto trend test + paper bot (BTC, ETH). FAKE money only: no exchange, no keys, no orders.
Rules and verdict are locked in CRYPTO_TREND_SPEC.md (written before the data was pulled).
  MODE=backtest  -> crypto_trend_report.json + Telegram summary
  MODE=live      -> replays the same rule from the live start date on fresh daily closes, alerts new trades,
                    writes crypto_trend_state.json. Replaying from scratch each run means a missed run heals itself."""
import json, math, os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone
import numpy as np, pandas as pd

COINS = ["BTC-USD", "ETH-USD"]
COST = 0.004            # per side: 0.1% fee + 0.3% slippage
STOP, MAXDAYS, FAST, SLOW = 0.90, 120, 20, 50
BANK = 1000.0
STATE = "crypto_trend_state.json"
REPORT = "crypto_trend_report.json"
TOKEN, CHAT = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")


def tg(text):
    print(text)
    if not (TOKEN and CHAT):
        return
    try:
        d = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=d), timeout=20)
    except Exception as e:
        print("telegram failed:", e)


def download():
    import yfinance as yf
    px = yf.download(COINS, start="2014-01-01", auto_adjust=True, progress=False)["Close"][COINS]
    today = datetime.now(timezone.utc).date()
    px = px[[d.date() < today for d in px.index]]          # drop today's unfinished bar
    return px


# ---------- one coin, one sub-wallet ----------
def sim_coin(close, cash, cost=COST, delay=0, trade_from=None):
    """close: pd.Series (no NaN). Returns (equity Series, trades list). Trading starts at trade_from (indicators use all history)."""
    c = close.values
    d = close.index
    m20 = close.rolling(FAST).mean().values
    m50 = close.rolling(SLOW).mean().values
    start = 0 if trade_from is None else int(np.searchsorted(d, pd.Timestamp(trade_from)))
    eq = np.full(len(c), np.nan)
    units, entry, days, held = 0.0, 0.0, 0, False
    pending = None                                            # (fill_index, side, reason, signal_date)
    trades, cur = [], None

    def buy(i, sig_date):
        nonlocal units, entry, days, held, cash, cur
        price = c[i] * (1 + cost)
        units, entry, days, held = cash / price, price, 0, True
        cur = {"coin": close.name, "signal": str(sig_date.date()), "entry_date": str(d[i].date()),
               "entry": round(price, 2), "stake": round(cash, 2)}
        cash = 0.0

    def sell(i, reason):
        nonlocal units, held, cash, cur
        price = c[i] * (1 - cost)
        cash = units * price
        cur.update(exit_date=str(d[i].date()), exit=round(price, 2), reason=reason,
                   ret_pct=round((cash / cur["stake"] - 1) * 100, 2))
        trades.append(cur)
        units, held, cur = 0.0, False, None

    for i in range(len(c)):
        if pending and pending[0] == i:
            _, side, why, sd = pending
            pending = None
            buy(i, sd) if side == "BUY" else sell(i, why)
        if i < start:
            eq[i] = cash
            continue
        if held:
            days += 1 if d[i] > pd.Timestamp(cur["entry_date"]) else 0
        order = None
        if pending is None and i >= SLOW:
            if held and d[i] > pd.Timestamp(cur["entry_date"]):
                if c[i] <= entry * STOP: order = ("SELL", "stop")
                elif c[i] < m20[i]: order = ("SELL", "trend")
                elif days >= MAXDAYS: order = ("SELL", "time")
            elif not held:
                if m20[i] > m50[i] and m20[i - 1] <= m50[i - 1] and c[i] > m50[i]:
                    order = ("BUY", None)
        if order:
            if delay == 0:
                buy(i, d[i]) if order[0] == "BUY" else sell(i, order[1])
            elif i + delay < len(c):
                pending = (i + delay, order[0], order[1], d[i])
        eq[i] = cash + units * c[i]
    if cur:
        cur.update(open=True, mark=round(c[-1], 2), ret_pct=round((units * c[-1] / cur["stake"] - 1) * 100, 2))
        trades.append(cur)
    return pd.Series(eq, index=d), trades


def hold_coin(close, cash, cost=COST):
    return cash / (close.iloc[0] * (1 + cost)) * close


def portfolio(px, coins, start, end=None, cost=COST, delay=0):
    """Equal sub-wallets; indicators warm up on data before `start`; trading only inside the window."""
    strat, hold, trades = [], [], []
    for k in coins:
        s = px[k].dropna()
        s = s[s.index <= pd.Timestamp(end)] if end else s
        if s.index[0] > pd.Timestamp(start):
            raise ValueError(f"{k} starts after {start}")
        e, t = sim_coin(s, BANK / len(coins), cost, delay, trade_from=start)
        w = s[s.index >= pd.Timestamp(start)]
        strat.append(e[w.index])
        hold.append(hold_coin(w, BANK / len(coins), cost))
        trades += [x for x in t if x["entry_date"] >= str(start)]
    return pd.concat(strat, axis=1).sum(axis=1), pd.concat(hold, axis=1).sum(axis=1), trades


# ---------- metrics ----------
def sharpe(r):
    r = r.dropna()
    return float(r.mean() / r.std() * math.sqrt(365)) if r.std() > 0 else 0.0


def stats(eq, trades=None, invested=None):
    r = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    dd = (eq / eq.cummax() - 1).min()
    out = {"from": str(eq.index[0].date()), "to": str(eq.index[-1].date()), "years": round(yrs, 1),
           "return_pct": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 1),
           "cagr_pct": round(((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100, 1) if yrs > 0 else None,
           "sharpe": round(sharpe(r), 2), "max_dd_pct": round(dd * 100, 1)}
    if trades is not None:
        closed = [t for t in trades if not t.get("open")]
        out.update(trades=len(closed), win_pct=round(100 * sum(t["ret_pct"] > 0 for t in closed) / len(closed), 1) if closed else None,
                   avg_trade_pct=round(float(np.mean([t["ret_pct"] for t in closed])), 2) if closed else None)
    if invested is not None:
        out["days_invested_pct"] = round(invested, 1)
    return out


def boot_diff(a, b, n=2000, block=30, seed=11):
    ra, rb = a.pct_change().dropna().values, b.pct_change().dropna().values
    L = len(ra)
    rng = np.random.default_rng(seed)
    nb = math.ceil(L / block)
    diffs = []
    for _ in range(n):
        st = rng.integers(0, L - block, nb)
        idx = np.concatenate([np.arange(s, s + block) for s in st])[:L]
        diffs.append(sharpe(pd.Series(ra[idx])) - sharpe(pd.Series(rb[idx])))
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return [round(float(lo), 2), round(float(hi), 2)]


def rolling(a, b, win=730, step=30):
    beat_r = beat_s = tot = 0
    for s in range(0, len(a) - win, step):
        x, y = a.iloc[s:s + win + 1], b.iloc[s:s + win + 1]
        tot += 1
        beat_r += x.iloc[-1] / x.iloc[0] > y.iloc[-1] / y.iloc[0]
        beat_s += sharpe(x.pct_change()) > sharpe(y.pct_change())
    return {"windows": tot, "beats_hold_return_pct": round(100 * beat_r / tot, 1) if tot else None,
            "beats_hold_sharpe_pct": round(100 * beat_s / tot, 1) if tot else None}


def invested_pct(px, coins, start, trades, end):
    days = pd.date_range(start, end, freq="D")
    inv = 0
    for t in trades:
        a = pd.Timestamp(t["entry_date"])
        b = pd.Timestamp(t.get("exit_date", end))
        inv += ((days >= a) & (days < b)).sum()
    return 100 * inv / (len(days) * len(coins))


def run(px, coins, start, cost=COST, delay=0, end=None):
    s, h, t = portfolio(px, coins, start, end, cost, delay)
    return {"strategy": stats(s, t, invested_pct(px, coins, start, t, s.index[-1])), "hold": stats(h)}, s, h, t


def backtest():
    px = download()
    last = str(px.index[-1].date())
    P = "2018-01-01"
    rep = {"spec": "CRYPTO_TREND_SPEC.md", "data_to": last, "cost_per_side": COST}
    prim, s, h, trades = run(px, COINS, P)
    prim["sharpe_diff_ci95"] = boot_diff(s, h)
    prim["rolling_2y"] = rolling(s, h)
    rep["primary"] = prim
    rep["stress_A_double_cost"] = run(px, COINS, P, cost=2 * COST)[0]
    rep["stress_B_one_day_late"] = run(px, COINS, P, delay=1)[0]
    rep["periods"] = {}
    for name, a, b in [("2018-19", "2018-01-01", "2019-12-31"), ("2020-21", "2020-01-01", "2021-12-31"),
                       ("2022", "2022-01-01", "2022-12-31"), ("2023-26", "2023-01-01", None)]:
        rep["periods"][name] = run(px, COINS, a, end=b)[0]
    rep["btc_alone_2014"] = run(px, ["BTC-USD"], "2014-11-10")[0]          # first date with 50 days of history
    rep["eth_alone_2017"] = run(px, ["ETH-USD"], "2017-12-29")[0]
    rep["trades"] = trades

    S, H = prim["strategy"], prim["hold"]
    dd_better = S["max_dd_pct"] - H["max_dd_pct"] >= 15
    sh_ok = S["sharpe"] >= H["sharpe"]
    stress_ok = all(rep[k]["strategy"]["sharpe"] >= 0.9 * rep[k]["hold"]["sharpe"]
                    for k in ("stress_A_double_cost", "stress_B_one_day_late"))
    verdict = "PASS" if (sh_ok and dd_better and stress_ok) else "WEAK" if dd_better else "FAIL"
    ci = prim["sharpe_diff_ci95"]
    rep["verdict"] = verdict
    rep["distinguishable"] = not (ci[0] <= 0 <= ci[1])
    with open(REPORT, "w") as f:
        json.dump(rep, f, indent=1)

    def line(lbl, r):
        a, b = r["strategy"], r["hold"]
        return (f"{lbl}: strategy {a['return_pct']:+.0f}% (Sharpe {a['sharpe']}, worst drop {a['max_dd_pct']}%) "
                f"vs hold {b['return_pct']:+.0f}% (Sharpe {b['sharpe']}, worst drop {b['max_dd_pct']}%)")
    msg = [f"CRYPTO TREND TEST (BTC+ETH, paper, data to {last})", f"VERDICT: {verdict}",
           line("2018-now", prim), line("Costs x2", rep["stress_A_double_cost"]), line("1 day late", rep["stress_B_one_day_late"]),
           f"Sharpe gap 95% range {ci} -> " + ("real difference" if rep["distinguishable"] else "not distinguishable from holding"),
           f"2-year windows: beats hold on return {prim['rolling_2y']['beats_hold_return_pct']}%, on Sharpe {prim['rolling_2y']['beats_hold_sharpe_pct']}%",
           f"Trades {S['trades']}, win {S['win_pct']}%, invested {S['days_invested_pct']}% of days"]
    for k, v in rep["periods"].items():
        msg.append(line(k, v))
    tg("\n".join(msg))


# ---------- live paper bot ----------
def live():
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    px = download()
    last = px.index[-1]
    if "start" not in st:
        st["start"] = str(last.date())                   # first complete day: trading begins after this close
        st["alerted"] = []
        tg(f"[CRYPTO TREND] paper bot started. BTC+ETH, ${BANK:,.0f} fake, rules in CRYPTO_TREND_SPEC.md. "
           "It only buys on a NEW 20/50-day cross, so it may sit in cash for weeks.")
    s, h, trades = portfolio(px, COINS, st["start"])
    for t in trades:
        for kind in ("BUY", "SELL"):
            if kind == "SELL" and t.get("open"):
                continue
            key = f"{t['coin']}|{t['entry_date']}|{kind}"
            if key in st["alerted"]:
                continue
            st["alerted"].append(key)
            if kind == "BUY":
                tg(f"[CRYPTO TREND] PAPER BUY {t['coin']} ${t['stake']:,.2f} @ {t['entry']:,.2f} ({t['entry_date']})")
            else:
                tg(f"[CRYPTO TREND] PAPER SELL {t['coin']} ({t['reason']}) @ {t['exit']:,.2f} -> {t['ret_pct']:+.1f}% ({t['exit_date']})")
    eq, hv = float(s.iloc[-1]), float(h.iloc[-1])
    st.update(last_date=str(last.date()), equity=round(eq, 2), hold_equity=round(hv, 2),
              open=[t for t in trades if t.get("open")], closed=[t for t in trades if not t.get("open")])
    if last.weekday() == 6 or os.environ.get("FORCE_SUMMARY") == "true":   # Sunday's close -> weekly summary
        pos = ", ".join(f"{t['coin']} {t['ret_pct']:+.1f}%" for t in st["open"]) or "all cash"
        tg(f"[CRYPTO TREND] week to {last.date()}: paper ${eq:,.2f} ({(eq/BANK-1)*100:+.1f}%) vs holding BTC+ETH "
           f"${hv:,.2f} ({(hv/BANK-1)*100:+.1f}%). Open: {pos}. Closed trades: {len(st['closed'])}.")
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1)


if __name__ == "__main__":
    (live if os.environ.get("MODE", "backtest") == "live" else backtest)()
