"""Trend-following backtest per TREND_SPEC.md (locked before data was pulled). Backtest only."""
import json, math
import numpy as np, pandas as pd

TICK = ["SPY","EFA","EWJ","IEF","TLT","GLD","USO","CPER","CORN","UNG","EURUSD=X","GBPUSD=X","USDJPY=X"]
FX = {"EURUSD=X","GBPUSD=X","USDJPY=X"}
START = "2003-01-01"
LB, VW, VT, CAP, MINH = 252, 60, 0.10, 2.0, 312
ETF_BPS, FX_PIP, BORROW = 2e-4, 0.75, 0.005
PIP = lambda t: 0.01 if t.endswith("JPY=X") else 0.0001
RNG = np.random.default_rng(7)


def download():
    import yfinance as yf
    return yf.download(TICK, start=START, auto_adjust=True, progress=False)["Close"][TICK]


def run(px, cost):
    r = px.pct_change()
    lr = np.log(px)
    n = len(px)
    live = px.notna().cumsum() >= MINH
    sig = np.sign(lr - lr.shift(LB))
    vol = r.rolling(VW).std() * math.sqrt(252)
    idx = px.index
    month_end = [i for i in range(n - 1) if idx[i].month != idx[i + 1].month]
    W = pd.DataFrame(0.0, index=idx, columns=px.columns)   # weights held at close of each day
    cur = pd.Series(0.0, index=px.columns)
    nxt = {}
    for e in month_end:
        ok = live.iloc[e] & sig.iloc[e].notna() & vol.iloc[e].notna() & (vol.iloc[e] > 0)
        k = int(ok.sum())
        w = pd.Series(0.0, index=px.columns)
        if k:
            w[ok] = sig.iloc[e][ok] * np.minimum(CAP, VT / vol.iloc[e][ok]) / k
        nxt[e + 1] = w                                   # executes at the close of day e+1
    for i in range(n):
        if i in nxt:
            cur = nxt[i]
        W.iloc[i] = cur
    # position held during day i is the weight set at close of i-1 (so the execution-day close does not earn the new weight)
    Wh = W.shift(1).fillna(0.0)
    gross = (Wh * r.fillna(0.0))
    turn = (W - W.shift(1).fillna(0.0)).abs()
    cst = pd.DataFrame(0.0, index=idx, columns=px.columns)
    if cost:
        for t in px.columns:
            unit = FX_PIP * PIP(t) / px[t] if t in FX else ETF_BPS
            cst[t] = turn[t] * unit
            if t not in FX:
                cst[t] += (-Wh[t]).clip(lower=0) * BORROW / 252
    net = gross - cst
    return net, Wh, live


def stats(d, years=None):
    d = d.dropna()
    if len(d) < 60:
        return None
    yrs = len(d) / 252
    ann, vol = d.mean() * 252, d.std() * math.sqrt(252)
    sr = ann / vol if vol > 0 else float("nan")
    se = math.sqrt((1 + 0.5 * sr ** 2) / yrs)
    cum = d.cumsum()
    dd = cum - cum.cummax()
    under = (dd < -1e-12).astype(int)
    longest, run_ = 0, 0
    last = None
    for ts, u in under.items():
        run_ = run_ + 1 if u else 0
        longest = max(longest, run_)
    # block bootstrap on monthly returns, 12-month blocks
    m = d.groupby([d.index.year, d.index.month]).sum().values
    bs = []
    if len(m) >= 36:
        nb = math.ceil(len(m) / 12)
        for _ in range(3000):
            st = RNG.integers(0, len(m) - 11, nb)
            x = np.concatenate([m[s:s + 12] for s in st])[:len(m)]
            s_ = x.std(ddof=1)
            bs.append(x.mean() * 12 / (s_ * math.sqrt(12)) if s_ > 0 else 0)
    return {"years": round(yrs, 1), "sharpe": round(sr, 2), "ci_analytic": [round(sr - 1.96 * se, 2), round(sr + 1.96 * se, 2)],
            "ci_block_boot": [round(float(np.percentile(bs, 2.5)), 2), round(float(np.percentile(bs, 97.5)), 2)] if bs else None,
            "ann_return_pct": round(ann * 100, 2), "ann_vol_pct": round(vol * 100, 2),
            "return_at_10pct_vol_pct": round(ann / vol * 10, 2) if vol > 0 else None,
            "max_dd_pct": round(float(dd.min()) * 100, 2), "longest_dd_trading_days": int(longest), "longest_dd_months": round(longest / 21, 1)}


def main():
    px = download().dropna(how="all")
    out = {"data_start": str(px.index[0].date()), "data_end": str(px.index[-1].date()),
           "first_valid": {t: str(px[t].first_valid_index().date()) for t in px.columns}}
    nets = {}
    for name, c in (("realistic", True), ("zero_cost", False)):
        net, Wh, live = run(px, c)
        nets[name] = (net, Wh, live)
        port = net.sum(axis=1)
        started = live.any(axis=1)
        port = port[started]
        out[name] = {"all": stats(port),
                     "2003-09": stats(port[port.index.year <= 2009]), "2010-19": stats(port[(port.index.year >= 2010) & (port.index.year <= 2019)]),
                     "2020-26": stats(port[port.index.year >= 2020])}
        full = live.all(axis=1)
        out[name]["all_13_live"] = stats(port[full[port.index]]) if full.any() else None
        out[name]["all_13_live_start"] = str(full[full].index[0].date()) if full.any() else None
    net, Wh, live = nets["realistic"]
    port = net.sum(axis=1)[live.any(axis=1)]
    out["by_market"] = {}
    for t in px.columns:
        s = net[t][live[t]]
        st = stats(s * 1.0)
        out["by_market"][t] = {"contribution_ann_pct": round(float(net[t][port.index].mean() * 252 * 100), 3),
                               "standalone_sharpe": st["sharpe"] if st else None, "years_live": st["years"] if st else None,
                               "avg_abs_weight": round(float(Wh[t][live[t]].abs().mean()), 3)}
    out["avg_gross_leverage"] = round(float(Wh.abs().sum(axis=1)[live.any(axis=1)].mean()), 2)
    ann = port.groupby(port.index.year).sum()
    out["calendar_year_pct"] = {int(k): round(float(v) * 100, 2) for k, v in ann.items()}
    json.dump(out, open("trend_report.json", "w"), indent=1)
    port.to_csv("trend_daily.csv", header=["net_return"])
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
