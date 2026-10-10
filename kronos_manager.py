"""Kronos Agent Manager = Observer + Critic + Reflector (see KRONOS_SPEC.md Section 7). ADVISORY ONLY: reads logs, writes report files, never touches books or trades.
Everything is filtered to data that existed on the as-of date (PIT guard). Reflections are template fills from report rows with status OK; nothing else is ever written as a conclusion."""
import json, math, os, sys
import numpy as np
import pandas as pd

H = 5
MIN_GATE, MIN_REGIME, MIN_DATES = 30, 15, 10
GATES = ["kronos_signal", "ftfc", "xgb", "fvg"]
BASKETS = {"ALL": None, "CRYPTO": {"BTC-USD", "ETH-USD", "SOL-USD"}, "EQUITY": {"SPY", "QQQ", "IWM"}}
REGIMES = ["ALL", "trend", "chop", "high_vol"]
XGB_P = 0.60
LEDGER_F = "kronos_ledger.json"
RNG = np.random.default_rng(11)
MARGIN = 0.15
CRYPTO_SKIP = 1


# ------------------------------------------------------------ rows (PIT)
def regime_of(d, T):
    """high_vol / trend / chop from bars up to and including T only."""
    d = d[d.index <= T]
    c, h, l = d["close"], d["high"], d["low"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atrp = (tr.ewm(alpha=1 / 14, adjust=False).mean() / c)
    win = atrp.iloc[-365:]
    if len(win) < 60 or np.isnan(atrp.iloc[-1]):
        return None
    if atrp.iloc[-1] >= np.nanpercentile(win.values, 75):
        return "high_vol"
    r20 = c.iloc[-1] / c.iloc[-21] - 1 if len(c) > 21 else np.nan
    if np.isnan(r20):
        return None
    return "trend" if abs(r20) >= atrp.iloc[-1] * math.sqrt(20) else "chop"


def fvg_fill(d, T, limit, sk=0):
    """Simulate the limit on the 2 real tradable bars (after any skipped bar). Returns (filled, fill_price)."""
    fut = d[d.index > T].iloc[sk:sk + 2]
    for _, b in fut.iterrows():
        if b["open"] <= limit: return True, float(b["open"])
        if b["low"] <= limit: return True, float(limit)
    return False, None


def matured_rows(fcs, dfs, as_of):
    out = []
    as_of = pd.Timestamp(as_of)
    for r in fcs:
        a = r["asset"]; T = pd.Timestamp(r["date"])
        if r.get("timing") != "v2" or r.get("late_run"): continue     # pre-fix crypto rows and rows made after the next open are not scored
        sk = CRYPTO_SKIP if a in BASKETS["CRYPTO"] else 0
        d = dfs[a][dfs[a].index <= as_of]                  # PIT: nothing after as_of is visible
        fut = d[d.index > T]
        if len(fut) < H + sk: continue                      # outcome not yet realized by as_of
        o1, c5 = float(fut["open"].iloc[sk]), float(fut["close"].iloc[sk + H - 1])
        row = dict(r); row["ret"] = c5 / o1 - 1; row["matured"] = str(fut.index[sk + H - 1].date())
        row["regime"] = regime_of(d, T)
        if r.get("fvg_limit") is not None:
            f, px = fvg_fill(d, T, r["fvg_limit"], sk); row["fvg_filled"] = f
            row["ret_from_fill"] = (c5 / px - 1) if f else None
        else:
            row["fvg_filled"] = None; row["ret_from_fill"] = None
        out.append(row)
    return out


# ------------------------------------------------------------ gate attribution
def split(rows, gate):
    if gate == "kronos_signal":
        return [r for r in rows if r["signal"]], [r for r in rows if not r["signal"]]
    sig = [r for r in rows if r["signal"]]
    if gate == "ftfc":
        return [r for r in sig if r["ftfc"]], [r for r in sig if not r["ftfc"]]
    if gate == "xgb":
        s2 = [r for r in sig if r.get("xgb_p") is not None]
        return [r for r in s2 if r["xgb_p"] > XGB_P], [r for r in s2 if r["xgb_p"] <= XGB_P]
    if gate == "fvg":
        return [r for r in sig if r.get("fvg_limit") is not None], [r for r in sig if r.get("fvg_limit") is None]


def cluster_ci(acc, rej):
    dates = sorted({r["date"] for r in acc + rej})
    if len(dates) < 2: return None, None
    A = {d: [r["ret"] for r in acc if r["date"] == d] for d in dates}; R = {d: [r["ret"] for r in rej if r["date"] == d] for d in dates}
    gaps = []
    for _ in range(2000):
        pick = RNG.choice(len(dates), len(dates))
        a = [x for i in pick for x in A[dates[i]]]; b = [x for i in pick for x in R[dates[i]]]
        if a and b: gaps.append(np.mean(a) - np.mean(b))
    if len(gaps) < 200: return None, None
    return float(np.percentile(gaps, 2.5)), float(np.percentile(gaps, 97.5))


def pct(x): return "n/a" if x is None else f"{x*100:+.2f}%"


def gate_table(rows, as_of, n_prop):
    out = []
    for g in GATES:
        for bk, members in BASKETS.items():
            rb = [r for r in rows if members is None or r["asset"] in members]
            acc, rej = split(rb, g)
            ga, gr_ = len(acc), len(rej)
            da, dr = len({r["date"] for r in acc}), len({r["date"] for r in rej})
            gate_ok = ga >= MIN_GATE and gr_ >= MIN_GATE and da >= MIN_DATES and dr >= MIN_DATES
            for rg in REGIMES:
                if rg == "ALL": a_, r_ = acc, rej
                else: a_, r_ = [r for r in acc if r["regime"] == rg], [r for r in rej if r["regime"] == rg]
                na, nr = len(a_), len(r_)
                ok = gate_ok if rg == "ALL" else (gate_ok and na >= MIN_REGIME and nr >= MIN_REGIME)
                row = {"as_of_date": str(as_of), "gate": g, "basket": bk, "regime": rg, "n_accepted": na, "n_rejected": nr,
                       "distinct_dates_accepted": len({r["date"] for r in a_}), "distinct_dates_rejected": len({r["date"] for r in r_}),
                       "mean_ret_accepted": None, "mean_ret_rejected": None, "gap": None, "ci_low": None, "ci_high": None,
                       "status": "OK" if ok else "INSUFFICIENT_DATA", "reflection": "", "proposal_count_to_date": n_prop}
                if ok:
                    ma, mr = float(np.mean([r["ret"] for r in a_])), float(np.mean([r["ret"] for r in r_]))
                    lo, hi = cluster_ci(a_, r_)
                    row.update(mean_ret_accepted=round(ma, 5), mean_ret_rejected=round(mr, 5), gap=round(ma - mr, 5),
                               ci_low=None if lo is None else round(lo, 5), ci_high=None if hi is None else round(hi, 5))
                    row["reflection"] = (f"Gate {g} ({bk}, {rg}) rejected {nr} signals with mean realized 5-day return {pct(mr)}; accepted {na} with mean {pct(ma)}; "
                                         f"gap {pct(ma - mr)} (95% CI {pct(lo)} to {pct(hi)}).")
                else:
                    row["reflection"] = (f"INSUFFICIENT_DATA in {rg} ({bk}): accepted {na}, rejected {nr}, dates {row['distinct_dates_accepted']}/{row['distinct_dates_rejected']}; "
                                         f"needs {MIN_GATE}/{MIN_GATE} per gate with {MIN_DATES}+ dates a side" + ("" if rg == "ALL" else f" and {MIN_REGIME}/{MIN_REGIME} in the bucket") + ".")
                out.append(row)
    return out


def fvg_table(rows):
    sig = [r for r in rows if r["signal"] and r.get("fvg_limit") is not None]
    filled = [r for r in sig if r["fvg_filled"]]
    return [{"n_signals_with_fvg": len(sig), "n_filled": len(filled), "fill_rate": round(len(filled) / len(sig), 3) if sig else None,
             "mean_ret_from_fill": round(float(np.mean([r["ret_from_fill"] for r in filled])), 5) if filled else None,
             "mean_ret_from_open_same_rows": round(float(np.mean([r["ret"] for r in filled])), 5) if filled else None,
             "mean_ret_from_open_missed_rows": round(float(np.mean([r["ret"] for r in sig if not r["fvg_filled"]])), 5) if len(sig) > len(filled) else None}]


def wilson(k, n, z=1.96):
    if n == 0: return None, None
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - w) / d, (c + w) / d


def accuracy_table(rows):
    out = []
    for bk, members in BASKETS.items():
        rb = [r for r in rows if members is None or r["asset"] in members]
        n = len(rb); hits = sum(1 for r in rb if (r["mean"] > 0) == (r["ret"] > 0)); up = sum(1 for r in rb if r["ret"] > 0)
        lo, hi = wilson(hits, n)
        sg = [r["ret"] for r in rb if r["signal"]]
        out.append({"basket": bk, "n_matured": n, "distinct_dates": len({r["date"] for r in rb}), "kronos_direction_hit_rate": round(hits / n, 3) if n else None,
                    "hit_ci_low": None if lo is None else round(lo, 3), "hit_ci_high": None if hi is None else round(hi, 3),
                    "share_of_up_outcomes_always_up_hit_rate": round(up / n, 3) if n else None,
                    "n_signals": len(sg), "mean_ret_when_signal": round(float(np.mean(sg)), 5) if sg else None})
    return out


# ------------------------------------------------------------ books
def curve_df(state, as_of):
    c = pd.DataFrame(state["curve"])
    if c.empty: return c
    c["date"] = pd.to_datetime(c["date"]); c = c[c["date"] <= pd.Timestamp(as_of)].set_index("date")
    return c


def sharpe(r):
    r = np.asarray(r, float)
    return float(r.mean() / r.std(ddof=1) * math.sqrt(365)) if len(r) > 2 and r.std(ddof=1) > 0 else None


def sharpe_gap_ci(r1, r2, block=5, n=2000):
    r1, r2 = np.asarray(r1), np.asarray(r2); m = min(len(r1), len(r2))
    if m < 20: return None, None, None
    r1, r2 = r1[-m:], r2[-m:]
    pt = (sharpe(r1) or 0) - (sharpe(r2) or 0); gaps = []; nb = math.ceil(m / block)
    for _ in range(n):
        st = RNG.integers(0, max(m - block + 1, 1), nb); idx = np.concatenate([np.arange(s, s + block) for s in st])[:m]
        a, b = sharpe(r1[idx]), sharpe(r2[idx])
        if a is not None and b is not None: gaps.append(a - b)
    if len(gaps) < 200: return pt, None, None
    return pt, float(np.percentile(gaps, 2.5)), float(np.percentile(gaps, 97.5))


def book_table(state, as_of):
    c = curve_df(state, as_of); out = []
    if c.empty or len(c) < 3: return out
    rets = c.pct_change().dropna()
    for k in [x for x in "ABCDEF" if x in c.columns]:
        r = rets[k].values; eq = c[k]; dd = float((eq / eq.cummax() - 1).min())
        sr = sharpe(r); yrs = len(r) / 365
        closed = [t for t in state["books"][k]["closed"] if t["exit_date"] <= str(pd.Timestamp(as_of).date())] if k in state["books"] else []
        row = {"book": k, "as_of_date": str(pd.Timestamp(as_of).date()), "days": len(r), "equity": float(eq.iloc[-1]), "return_pct": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2),
               "sharpe_annualized": None if sr is None else round(sr, 2), "sharpe_std_error": None if sr is None else round(math.sqrt((1 + 0.5 * sr * sr) / max(yrs, 1e-9)), 2),
               "max_drawdown_pct": round(dd * 100, 2), "closed_trades": len(closed),
               "stop_exit_share": round(sum(1 for t in closed if t["reason"] == "stop") / len(closed), 3) if closed else None}
        if k in state["books"]:
            b = state["books"][k]; row["fills_cumulative_at_state_time"] = b["fills"]; row["missed_cumulative_at_state_time"] = b["missed"]
        for ref in ("A", "F"):
            if k != ref and ref in rets.columns:
                pt, lo, hi = sharpe_gap_ci(rets[k].values, rets[ref].values)
                row[f"sharpe_gap_vs_{ref}"] = None if pt is None else round(pt, 2); row[f"gap_vs_{ref}_ci_low"] = None if lo is None else round(lo, 2); row[f"gap_vs_{ref}_ci_high"] = None if hi is None else round(hi, 2)
        out.append(row)
    return out


# ------------------------------------------------------------ ledger and forward test (Option B + stat bar)
def load_ledger():
    return json.load(open(LEDGER_F)) if os.path.exists(LEDGER_F) else []


def window_for(prop_date):
    d = pd.Timestamp(prop_date); start = (d + pd.offsets.MonthBegin(1)).normalize(); end = start + pd.offsets.MonthEnd(0)
    return start, end


def eval_window(state, p, start, end, as_of):
    c = curve_df(state, as_of)
    if c.empty or pd.Timestamp(as_of) < end: return {"status": "WINDOW_NOT_COMPLETE"}
    w = c[(c.index >= start) & (c.index <= end)]; r = w.pct_change().dropna()
    t, k = p["test_book_id"], p["control_book_id"]
    if t not in w.columns or k not in w.columns or len(r) < 20: return {"status": "INSUFFICIENT_FORWARD_DATA"}
    trades = [x for x in state["books"][t]["closed"] if str(start.date()) <= x["entry_date"] <= str(end.date())]
    st, sc = sharpe(r[t].values), sharpe(r[k].values)
    pt, lo, hi = sharpe_gap_ci(r[t].values, r[k].values)
    res = {"start": str(start.date()), "end": str(end.date()), "sharpe_test": st, "sharpe_control": sc, "gap": None if st is None or sc is None else st - sc, "gap_ci_low": lo, "gap_ci_high": hi, "n_trades_test": len(trades)}
    if len(trades) < 15: res["status"] = "INSUFFICIENT_FORWARD_DATA"
    elif res["gap"] is not None and res["gap"] >= MARGIN: res["status"] = "PASS"
    else: res["status"] = "FAIL"
    return res


def evaluate_ledger(ledger, state, as_of):
    for p in ledger:
        s1, e1 = window_for(p["date"]); s2 = e1 + pd.Timedelta(days=1); e2 = s2 + pd.offsets.MonthEnd(0)
        w1 = eval_window(state, p, s1, e1, as_of); p["window1"] = w1
        if w1["status"] == "PASS":
            w2 = eval_window(state, p, s2, e2, as_of); p["window2"] = w2
            p["status"] = "CONFIRMED_IMPROVEMENT" if w2["status"] == "PASS" else ("PROVISIONAL_PASS" if w2["status"] == "WINDOW_NOT_COMPLETE" else "REJECTED_BY_FORWARD_TEST")
        elif w1["status"] == "FAIL": p["status"] = "REJECTED_BY_FORWARD_TEST"
        elif w1["status"] == "INSUFFICIENT_FORWARD_DATA": p["status"] = "INSUFFICIENT_FORWARD_DATA"
        else: p["status"] = p.get("status", "PENDING_WINDOW") if p.get("status") in (None, "PROPOSED", "PENDING_WINDOW") else p["status"]
    return ledger


# ------------------------------------------------------------ build + write
def build_report(dfs, fcs, state, ledger, as_of):
    as_of = pd.Timestamp(as_of)
    rows = matured_rows(fcs, dfs, as_of)
    n_prop = len(ledger)
    gates = gate_table(rows, as_of.date(), n_prop)
    okr = [g for g in gates if g["status"] == "OK" and g["ci_low"] is not None and g["ci_high"] is not None]
    excl = [g for g in okr if g["ci_low"] > 0 or g["ci_high"] < 0]
    looks = {"ok_rows_with_ci": len(okr), "ci_excludes_zero": len(excl), "expected_by_chance_at_5pct": round(0.05 * len(okr), 1),
             "note": "Rows overlap (the same signals appear in ALL, a basket and a regime), so these counts are a rough guide. If ci_excludes_zero is not clearly above the chance figure, treat every 'significant' gap as noise."}
    rep = {"as_of_date": str(as_of.date()), "matured_forecasts": len(rows), "multiple_looks": looks, "gates": gates, "fvg": fvg_table(rows),
           "accuracy": accuracy_table(rows), "books": book_table(state, as_of), "ledger": evaluate_ledger(ledger, state, as_of), "proposal_count_to_date": n_prop}
    return rep


def write(rep):
    pd.DataFrame(rep["gates"]).to_csv("kronos_report_gates.csv", index=False)
    pd.DataFrame(rep["books"]).to_csv("kronos_report_books.csv", index=False)
    pd.DataFrame(rep["accuracy"]).to_csv("kronos_report_accuracy.csv", index=False)
    pd.DataFrame(rep["fvg"]).to_csv("kronos_report_fvg.csv", index=False)
    json.dump(rep, open("kronos_report.json", "w"), indent=1, default=str)
    if rep["ledger"]: json.dump(rep["ledger"], open(LEDGER_F, "w"), indent=1, default=str)


if __name__ == "__main__":
    import datetime, kronos_system as K
    now = datetime.datetime.utcnow(); dfs = K.fetch(now)
    state = json.load(open(K.STATE_F)); fcs = json.load(open(K.FC_F))
    as_of = max(d.index[-1] for d in dfs.values())
    rep = build_report(dfs, fcs, state, load_ledger(), as_of); write(rep)
    ok = sum(1 for g in rep["gates"] if g["status"] == "OK")
    ml = rep["multiple_looks"]
    msg = (f"KRONOS manager | as of {rep['as_of_date']} | matured forecasts {rep['matured_forecasts']} | gate rows OK {ok}/{len(rep['gates'])} | "
           f"CI excludes zero in {ml['ci_excludes_zero']} of {ml['ok_rows_with_ci']} OK rows (about {ml['expected_by_chance_at_5pct']} expected by chance) | proposals {rep['proposal_count_to_date']}")
    print(msg); K.tg(msg)
