"""Long-only comparison + GLD-excluded + 2005+/20y-history cuts, per TREND_COMPARE_SPEC.md (locked before running)."""
import json, math
import numpy as np, pandas as pd
import trend_backtest as T

RNG = np.random.default_rng(11)
OLD_CUT = "2006-06-30"


def monthly(d):
    return d.groupby([d.index.year, d.index.month]).sum()


def sharpe_m(m):
    s = m.std(ddof=1)
    return m.mean() * 12 / (s * math.sqrt(12)) if s > 0 else 0.0


def delta_ci(a, b):
    ma, mb = monthly(a), monthly(b)
    ix = ma.index.intersection(mb.index)
    ma, mb = ma[ix].values, mb[ix].values
    n = len(ma)
    out = []
    nb = math.ceil(n / 12)
    for _ in range(3000):
        st = RNG.integers(0, n - 11, nb)
        idx = np.concatenate([np.arange(s, s + 12) for s in st])[:n]
        out.append(sharpe_m(pd.Series(ma[idx])) - sharpe_m(pd.Series(mb[idx])))
    return [round(float(np.percentile(out, 2.5)), 2), round(float(np.percentile(out, 97.5)), 2)]


def series(px, mode, start=None):
    net, Wh, live = T.run(px, True, mode)
    p = net.sum(axis=1)[live.any(axis=1)]
    if start:
        p = p[p.index >= start]
    return p


def block(px, start=None):
    res, ser = {}, {}
    for mode in ("trend", "lo_vt", "lo_eq"):
        p = series(px, mode, start); ser[mode] = p
        res[mode] = {"all": T.stats(p), "2003-09": T.stats(p[p.index.year <= 2009]),
                     "2010-19": T.stats(p[(p.index.year >= 2010) & (p.index.year <= 2019)]), "2020-26": T.stats(p[p.index.year >= 2020])}
    res["delta_vs_lo_vt"] = {"point": round(res["trend"]["all"]["sharpe"] - res["lo_vt"]["all"]["sharpe"], 2), "ci": delta_ci(ser["trend"], ser["lo_vt"])}
    res["delta_vs_lo_eq"] = {"point": round(res["trend"]["all"]["sharpe"] - res["lo_eq"]["all"]["sharpe"], 2), "ci": delta_ci(ser["trend"], ser["lo_eq"])}
    for per, f in (("2003-09", lambda i: i.year <= 2009), ("2010-19", lambda i: (i.year >= 2010) & (i.year <= 2019)), ("2020-26", lambda i: i.year >= 2020)):
        a, b = ser["trend"][f(ser["trend"].index)], ser["lo_vt"][f(ser["lo_vt"].index)]
        res["delta_vs_lo_vt_" + per] = round(T.stats(a)["sharpe"] - T.stats(b)["sharpe"], 2)
    return res


def main():
    px = T.download().dropna(how="all").ffill(limit=5)
    out = {"full": block(px)}
    out["ex_GLD"] = block(px.drop(columns=["GLD"]))
    old = [t for t in px.columns if px[t].first_valid_index() <= pd.Timestamp(OLD_CUT)]
    out["cut_2005_old_markets"] = block(px[old], start=pd.Timestamp("2005-01-01"))
    out["cut_2005_markets"] = old
    json.dump(out, open("trend_compare_report.json", "w"), indent=1)
    for k in ("full", "ex_GLD", "cut_2005_old_markets"):
        r = out[k]
        print(k, {m: (r[m]["all"]["sharpe"], r[m]["all"]["ci_block_boot"]) for m in ("trend", "lo_vt", "lo_eq")}, r["delta_vs_lo_vt"], r["delta_vs_lo_eq"])


if __name__ == "__main__":
    main()
