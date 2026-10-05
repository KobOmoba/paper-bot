"""FX pairs-trading (statistical arbitrage) BACKTEST ONLY - no bot, no orders. Free Yahoo daily data.
Rules are fixed in advance from the pasted pitch and are NOT tuned on this data (so every year is out-of-sample for the rules):
  spread = log(A) - a - h*log(B), with a,h from a rolling OLS over the previous HW days (hedge ratio refits daily, no look-ahead);
  z = spread / its in-window residual std;  enter when |z| > 2, exit when |z| < 0.2, stop when |z| > 3.5, time stop 30 days.
  Signals use the close of day t; fills are at the close of day t+1 (no look-ahead). Hedge ratio is frozen for each trade.
Gates tested: G0 none | G1 Engle-Granger cointegration p < 0.10 on the trailing 500 days (re-tested monthly) |
              G2 = G1 and OU half-life between 2 and 30 days.
Costs: retail-style spreads per leg (round trip) + a swap drag, scaled by a cost multiplier (0 = frictionless, 1 = retail, 0.3 = near-institutional, 2 = bad).
Return per trade is on gross exposure 1+|h| (unleveraged). Portfolio = equal capital split across the pairs."""
import json, math, sys
import numpy as np
import pandas as pd

HW, ZW_MIN = 250, 250
ENTRY, EXIT, STOP, MAX_HOLD = 2.0, 0.2, 3.5, 30
COINT_WIN, COINT_P, HL_MIN, HL_MAX = 500, 0.10, 2.0, 30.0
SWAP_BPS_PER_DAY = 0.3                  # assumption: drag per day held, in basis points of gross exposure at cost multiplier 1
START = "2005-01-01"
# (A, B) as Yahoo tickers; spread in pips per leg and pip size
PAIRS = [("AUDUSD=X", "NZDUSD=X"), ("EURUSD=X", "GBPUSD=X"), ("EURGBP=X", "CHFJPY=X"), ("AUDJPY=X", "NZDJPY=X"), ("EURUSD=X", "USDCHF=X")]
SPREAD_PIPS = {"AUDUSD=X": 1.2, "NZDUSD=X": 1.8, "EURUSD=X": 0.8, "GBPUSD=X": 1.2, "EURGBP=X": 1.5, "CHFJPY=X": 2.5,
               "AUDJPY=X": 2.0, "NZDJPY=X": 2.5, "USDCHF=X": 1.5}
PIP = lambda t: 0.01 if t.endswith("JPY=X") else 0.0001


def download():
    import yfinance as yf
    tick = sorted({t for p in PAIRS for t in p})
    df = yf.download(tick, start=START, auto_adjust=True, progress=False)["Close"]
    return df.dropna(how="all")


def half_life(e):
    e = np.asarray(e, float)
    de, lag = np.diff(e), e[:-1]
    X = np.vstack([np.ones_like(lag), lag]).T
    b = np.linalg.lstsq(X, de, rcond=None)[0][1]
    return -math.log(2) / math.log(1 + b) if -1 < b < 0 else float("inf")


def gates(la, lb, idx):
    """Return (g1, g2) boolean arrays: tradable on day i using only data up to i-1, re-tested at the start of each month."""
    from statsmodels.tsa.stattools import coint
    n = len(idx)
    g1, g2 = np.zeros(n, bool), np.zeros(n, bool)
    cur1 = cur2 = False
    last_month = None
    for i in range(COINT_WIN + 1, n):
        m = (idx[i].year, idx[i].month)
        if m != last_month:
            last_month = m
            a, b = la[i - COINT_WIN:i], lb[i - COINT_WIN:i]
            try:
                p = coint(a, b, trend="c", autolag="aic")[1]
            except Exception:
                p = 1.0
            cur1 = p < COINT_P
            h = np.polyfit(b, a, 1)
            hl = half_life(a - np.polyval(h, b))
            cur2 = cur1 and HL_MIN <= hl <= HL_MAX
        g1[i], g2[i] = cur1, cur2
    return g1, g2


def run_pair(df, A, B, gate, cost_mult):
    d = df[[A, B]].dropna()
    idx = d.index
    la, lb = np.log(d[A].values), np.log(d[B].values)
    sa, sb = pd.Series(la), pd.Series(lb)
    cov = sa.rolling(HW).cov(sb)
    varb = sb.rolling(HW).var()
    vara = sa.rolling(HW).var()
    h = (cov / varb).values
    a0 = (sa.rolling(HW).mean() - cov / varb * sb.rolling(HW).mean()).values
    sd = np.sqrt(np.maximum((vara - cov ** 2 / varb).values, 1e-18))
    z = (la - a0 - h * lb) / sd
    n = len(d)
    price_cost = lambda i, hh: ((SPREAD_PIPS[A] * PIP(A) / d[A].values[i]) + abs(hh) * (SPREAD_PIPS[B] * PIP(B) / d[B].values[i])) / (1 + abs(hh))
    daily = np.zeros(n)
    trades = []
    pos, hh, ent_i, acc = 0, 0.0, 0, 0.0
    pend = None
    for i in range(1, n):
        if pos != 0:                                     # earn today's move on the open position
            r = pos * ((la[i] - la[i - 1]) - hh * (lb[i] - lb[i - 1])) / (1 + abs(hh))
            daily[i] += r; acc += r
        if pend is not None:                             # execute yesterday's decision at today's close
            if pend == "exit" and pos != 0:
                held = i - ent_i
                cost = cost_mult * (price_cost(i, hh) + SWAP_BPS_PER_DAY * 1e-4 * held)
                daily[i] -= cost
                trades.append({"pair": f"{A[:6]}/{B[:6]}", "entry": str(idx[ent_i].date()), "exit": str(idx[i].date()), "days": held,
                               "dir": pos, "gross": acc, "net": acc - cost, "reason": why})
                pos = 0
            elif pend in (1, -1) and pos == 0 and not (np.isnan(h[i - 1]) or np.isnan(z[i - 1])):
                pos, hh, ent_i, acc = pend, h[i - 1], i, 0.0
            pend = None
        if np.isnan(z[i]):
            continue
        if pos != 0:
            why = None
            if abs(z[i]) < EXIT:
                why = "target"
            elif abs(z[i]) > STOP:
                why = "stop"
            elif i - ent_i >= MAX_HOLD:
                why = "time"
            if why:
                pend = "exit"
        elif gate[i] and abs(z[i]) > ENTRY and abs(z[i]) < STOP:
            pend = -1 if z[i] > 0 else 1                  # z high -> spread rich -> short A / long B (pos=-1)
    return pd.Series(daily, index=idx), trades


def metrics(daily_port, trades):
    r = daily_port.dropna()
    ann = r.mean() * 252
    vol = r.std() * math.sqrt(252)
    cum = r.cumsum()
    dd = float((cum - cum.cummax()).min())
    nets = np.array([t["net"] for t in trades]) if trades else np.array([])
    w, l = nets[nets > 0], nets[nets <= 0]
    return {"trades": int(len(nets)), "win_rate": round(float((nets > 0).mean()), 3) if len(nets) else None,
            "avg_win_pct": round(float(w.mean()) * 100, 3) if len(w) else None, "avg_loss_pct": round(float(l.mean()) * 100, 3) if len(l) else None,
            "profit_factor": round(float(w.sum() / -l.sum()), 2) if len(l) and l.sum() < 0 else None,
            "ann_return_pct": round(float(ann) * 100, 2), "ann_vol_pct": round(float(vol) * 100, 2),
            "sharpe": round(float(ann / vol), 2) if vol > 0 else None, "max_drawdown_pct": round(dd * 100, 2),
            "stop_outs": sum(1 for t in trades if t["reason"] == "stop"), "time_stops": sum(1 for t in trades if t["reason"] == "time")}


def main(df=None):
    df = download() if df is None else df
    out = {"data_start": str(df.index[0].date()), "data_end": str(df.index[-1].date()), "results": {}, "by_pair": {}, "by_period": {}}
    gate_cache = {}
    for A, B in PAIRS:
        d = df[[A, B]].dropna()
        gate_cache[(A, B)] = gates(np.log(d[A].values), np.log(d[B].values), d.index)
    all_trades = []
    for gname, gi in (("G0_no_gate", None), ("G1_cointegration", 0), ("G2_coint_plus_halflife", 1)):
        for cm in (0.0, 0.3, 1.0, 2.0):
            ser, trs = [], []
            for A, B in PAIRS:
                d = df[[A, B]].dropna()
                gate = np.ones(len(d), bool) if gi is None else gate_cache[(A, B)][gi]
                s, t = run_pair(df, A, B, gate, cm)
                ser.append(s); trs += t
                if cm == 1.0:
                    out["by_pair"].setdefault(gname, {})[f"{A[:6]}/{B[:6]}"] = metrics(s, t)
            port = pd.concat(ser, axis=1).fillna(0).mean(axis=1)
            out["results"][f"{gname}|cost_x{cm}"] = metrics(port, trs)
            if cm == 1.0:
                all_trades += [dict(t, gate=gname) for t in trs]
                for y0 in range(2007, 2027, 5):
                    sl = port[(port.index.year >= y0) & (port.index.year < y0 + 5)]
                    tt = [t for t in trs if y0 <= int(t["exit"][:4]) < y0 + 5]
                    if len(sl):
                        out["by_period"].setdefault(gname, {})[f"{y0}-{y0+4}"] = metrics(sl, tt)
    json.dump(out, open("fx_pairs_report.json", "w"), indent=1)
    pd.DataFrame(all_trades).to_csv("fx_pairs_trades.csv", index=False)
    for k, v in out["results"].items():
        print(k, v)
    return out


if __name__ == "__main__":
    main()
