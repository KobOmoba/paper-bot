"""Slow momentum, tested at several cost levels. PRE-REGISTERED before any run (no tuning afterwards):
rank liquid US stocks by 126-session (~6 month) return, hold the top 10 equal-weight (10% each), rebalance every
R sessions (R = 21 monthly, or 63 quarterly). Signal at the close, trade at the NEXT close. Costs are charged only on
the turnover actually traded. Cost per side: 0.1% (stock-level), 0.5% (mid), 1.5% (token-level, = 0.5% fee + 1% slippage).
Universe: the same 89 large caps as the other US tests (survivorship bias: flatters results)."""
import json, os, re, statistics, urllib.parse, urllib.request
import numpy as np
import pandas as pd

LOOK, N, MIN_VAL, H = 126, 10, 20_000_000, 756
RS = {"monthly (21d)": 21, "quarterly (63d)": 63}
COSTS = {"0.1% per side (stock)": 0.001, "0.5% per side (mid)": 0.005, "1.5% per side (token)": 0.015}
SPLIT = "2020-03-23"


def simulate(px, vol, R, cost):
    """px, vol: DataFrames (dates x tickers). Returns (equity Series, total one-way turnover)."""
    P = px.values
    ret = np.nan_to_num(px.pct_change().values, nan=0.0)
    dv = (px * vol).rolling(20).mean().values
    n, m = P.shape
    w = np.zeros(m)
    eq, equity, turn_total, pending = [1.0], 1.0, 0.0, None
    for i in range(1, n):
        day = float((w * ret[i]).sum())
        equity *= 1 + day
        if day > -1:
            w = w * (1 + ret[i]) / (1 + day)
        if pending is not None:
            turn = float(np.abs(pending - w).sum())
            equity *= 1 - turn * cost
            turn_total += turn
            w, pending = pending.copy(), None
        if i >= LOOK and i % R == 0:
            base, now = P[i - LOOK], P[i]
            ok = np.isfinite(base) & np.isfinite(now) & (base > 0) & np.isfinite(dv[i]) & (dv[i] >= MIN_VAL)
            score = np.where(ok, now / np.where(base > 0, base, 1) - 1, -np.inf)
            top = np.argsort(score)[::-1][:N]
            top = [j for j in top if np.isfinite(score[j])]
            tgt = np.zeros(m)
            for j in top:
                tgt[j] = 1.0 / N
            pending = tgt
        eq.append(equity)
    return pd.Series(eq, index=px.index), turn_total


def maxdd(x):
    x = np.asarray(x, float)
    return float((x / np.maximum.accumulate(x) - 1).min())


def metrics(eq, spy, years):
    out = {}
    for name, sl in (("build", eq.index <= SPLIT), ("test", eq.index > SPLIT)):
        e, s = eq[sl], spy[sl]
        out[name] = {"return_pct": round((e.iloc[-1] / e.iloc[0] - 1) * 100, 1),
                     "spy_pct": round((s.iloc[-1] / s.iloc[0] - 1) * 100, 1),
                     "max_dd_pct": round(maxdd(e) * 100, 1)}
    res = []
    for s0 in range(LOOK, len(eq) - H, 21):
        s1 = s0 + H
        res.append((eq.iloc[s1] / eq.iloc[s0] - 1, spy.iloc[s1] / spy.iloc[s0] - 1,
                    maxdd(eq.iloc[s0:s1 + 1]), maxdd(spy.iloc[s0:s1 + 1])))
    out["rolling_3y"] = {"windows": len(res),
                         "beats_spy_pct": round(100 * sum(r[0] > r[1] for r in res) / len(res), 1),
                         "median_strategy_pct": round(100 * statistics.median(r[0] for r in res), 1),
                         "median_spy_pct": round(100 * statistics.median(r[1] for r in res), 1),
                         "smaller_dd_pct": round(100 * sum(r[2] > r[3] for r in res) / len(res), 1),
                         "worst_strategy_pct": round(100 * min(r[0] for r in res), 1),
                         "worst_spy_pct": round(100 * min(r[1] for r in res), 1)}
    return out


def run(px, vol, spy):
    years = (px.index[-1] - px.index[0]).days / 365.25
    rep = {"universe": int(px.shape[1]), "first": str(px.index[0].date()), "last": str(px.index[-1].date()),
           "params": {"lookback": LOOK, "top_n": N, "rebalance": RS, "costs": COSTS}, "results": {}}
    for rn, R in RS.items():
        for cn, c in COSTS.items():
            eq, turn = simulate(px, vol, R, c)
            m = metrics(eq, spy, years)
            m["annual_turnover_x"] = round(turn / years, 1)
            rep["results"][f"{rn} | {cn}"] = m
    return rep


def summary(rep):
    L = [f"Slow momentum ({rep['universe']} stocks, {rep['first']} to {rep['last']})"]
    for k, m in rep["results"].items():
        r = m["rolling_3y"]
        L.append(f"{k}: test {m['test']['return_pct']}% (SPY {m['test']['spy_pct']}%), 3y windows beat SPY "
                 f"{r['beats_spy_pct']}%, median {r['median_strategy_pct']}% vs {r['median_spy_pct']}%, "
                 f"worst {r['worst_strategy_pct']}% vs {r['worst_spy_pct']}%, turnover {m['annual_turnover_x']}x/yr")
    return "\n".join(L)


def main():
    import yfinance as yf
    src = open("us_backtest.py").read()
    tick = re.search(r'TICKERS = """(.*?)"""', src, re.S).group(1).split()
    d = yf.download(tick + ["SPY"], start="2005-01-01", auto_adjust=True, group_by="ticker", progress=False, threads=True)
    close = pd.DataFrame({t: d[t]["Close"] for t in tick + ["SPY"] if t in d.columns.get_level_values(0)})
    volume = pd.DataFrame({t: d[t]["Volume"] for t in tick if t in d.columns.get_level_values(0)})
    close = close.dropna(how="all")
    volume = volume.reindex(close.index).fillna(0.0)
    spy = close["SPY"].ffill()
    px = close[[c for c in close.columns if c != "SPY"]]
    rep = run(px, volume[px.columns], spy)
    json.dump(rep, open("mom_report.json", "w"), indent=1)
    msg = summary(rep)
    print(msg)
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg[:4000]}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=20)


if __name__ == "__main__":
    main()
