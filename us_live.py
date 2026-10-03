"""Tokenized-stock QUARTERLY MOMENTUM tracker (PAPER ONLY). Tokenized stocks (Binance bStocks, Robinhood Stock Tokens)
track US stock prices, so we run the strategy on the underlying stocks and charge token-level costs.
Rule (identical to mom_rebalance.py, which was tested before running): every 63 sessions rank liquid stocks by
126-session return, hold the top 10 equal-weight. Signal at the close, trade at the NEXT close. Cost 1.5% per side
(0.5% fee + 1% slippage) charged on the value actually traded. No orders are ever placed. Runs after the US close.
Backtest caveats: universe is today's large caps (survivorship bias); worst drawdowns were about -55%."""
import json, os, traceback
import yfinance as yf
import ngx_bot as b

TICKERS = """AAPL MSFT AMZN GOOGL NVDA ORCL INTC CSCO IBM QCOM TXN ADBE AMGN GILD JNJ PFE MRK ABT LLY UNH CVS MDT BMY
JPM BAC WFC C GS MS AXP USB PNC COF KO PEP PG WMT COST MCD SBUX NKE DIS CMCSA T VZ XOM CVX COP SLB OXY EOG HAL
CAT DE BA HON GE MMM UPS FDX LMT RTX NOC GD UNP CSX NSC HD LOW TGT TJX F DUK SO NEE D EXC AEP MO PM CL KMB
MCK CI ISRG BIIB MU AMAT ADI TSLA META NFLX AMD AVGO CRM PYPL UBER""".split()
STATE = "us_state.json"
START, LOOK, N, REB, COST, MIN_DV, KEEP = 100_000.0, 126, 10, 63, 0.015, 20_000_000, 260
SCHEMA = "quarterly-v1"


def download(period):
    df = yf.download(TICKERS + ["SPY"], period=period, auto_adjust=True, group_by="ticker", progress=False, threads=True)
    out = {}
    for t in TICKERS + ["SPY"]:
        try:
            d = df[t].dropna(subset=["Close"])
        except Exception:
            continue
        out[t] = [(i.strftime("%Y-%m-%d"), float(r["Close"]), float(r["Volume"] or 0)) for i, r in d.iterrows()]
    return out


def signal(hist):
    """Top-N stocks by 126-session return among those with 20-day average dollar volume >= MIN_DV."""
    score = {}
    for sym, h in hist.items():
        if sym == "SPY" or len(h) < LOOK + 1:
            continue
        dv = sum(r[1] * r[2] for r in h[-20:]) / 20
        base = h[-1 - LOOK][1]
        if base > 0 and dv >= MIN_DV:
            score[sym] = h[-1][1] / base - 1
    top = sorted(score, key=score.get, reverse=True)[:N]
    return {sym: 1.0 / N for sym in top}, {sym: round(score[sym] * 100, 1) for sym in top}


def equity(a, px):
    return a["cash"] + sum(sh * px.get(sym, a["last_px"].get(sym, 0)) for sym, sh in a["pos"].items())


def seed(data):
    hist = {sym: [list(r) for r in rows][-KEEP:] for sym, rows in data.items() if rows}
    last = max(r[-1][0] for r in hist.values())
    pend, sc = signal(hist)
    a = {"cash": START, "pos": {}, "pending": pend, "since": 0, "last_px": {}, "spy0": hist["SPY"][-1][1],
         "start_date": last, "trades": 0, "costs_paid": 0.0, "log": []}
    return {"schema": SCHEMA, "hist": hist, "last_date": last, "acct": a}, sc


def process_day(s, date, say):
    a, hist = s["acct"], s["hist"]
    px = {sym: h[-1][1] for sym, h in hist.items() if h and h[-1][0] == date}
    a["last_px"].update(px)
    if a["pending"] is not None:
        eq = equity(a, px)
        tgt = a["pending"]
        scale = 1.0                                # size holdings so value traded + its cost fits inside equity
        for _ in range(8):
            traded, new_pos = 0.0, {}
            for sym in set(tgt) | set(a["pos"]):
                if sym not in px:                  # no price today: keep as is
                    if sym in a["pos"]:
                        new_pos[sym] = a["pos"][sym]
                    continue
                cur_val = a["pos"].get(sym, 0.0) * px[sym]
                want = eq * scale * tgt.get(sym, 0.0)
                traded += abs(want - cur_val)
                if want > 0:
                    new_pos[sym] = want / px[sym]
            cost = traded * COST
            invested = sum(sh * px.get(sym, a["last_px"].get(sym, 0)) for sym, sh in new_pos.items())
            scale *= (eq - cost) / invested if invested > 0 else 1.0
        a["pos"], a["cash"] = new_pos, max(eq - invested - cost, 0.0)
        a["costs_paid"] += cost
        a["trades"] += 1
        a["pending"] = None
        a["log"].append({"date": date, "event": "rebalance", "traded": round(traded), "cost": round(cost),
                         "holdings": sorted(new_pos)})
        say(f"REBALANCE (paper) {date}: now hold {', '.join(sorted(new_pos))}. Traded ${traded:,.0f}, cost ${cost:,.0f}.")
    a["since"] += 1
    if a["since"] >= REB and any(len(h) >= LOOK + 1 for h in hist.values()):
        a["pending"], sc = signal(hist)
        a["since"] = 0
        a["log"].append({"date": date, "event": "signal", "picks": sc})
        say(f"Rebalance signal {date}: next close buys " + ", ".join(f"{k} ({v:+.0f}% 6m)" for k, v in sc.items()))
    a["log"] = a["log"][-40:]


def digest(s, date):
    a, hist = s["acct"], s["hist"]
    px = {sym: h[-1][1] for sym, h in hist.items() if h}
    eq = equity(a, px)
    spy = px.get("SPY", a["spy0"]) / a["spy0"] - 1
    L = [f"Tokenized-stock QUARTERLY momentum (paper) {date}",
         f"Equity ${eq:,.0f} ({(eq / START - 1) * 100:+.1f}%) vs SPY {spy * 100:+.1f}% since {a['start_date']} | "
         f"cash ${a['cash']:,.0f} | costs paid ${a['costs_paid']:,.0f}",
         f"Sessions since last rebalance signal: {a['since']} of {REB}"]
    for sym, sh in sorted(a["pos"].items()):
        L.append(f"  {sym} ${sh * px.get(sym, 0):,.0f}")
    if a["pending"]:
        L.append("Queued for next close: " + ", ".join(sorted(a["pending"])))
    if not a["pos"] and not a["pending"]:
        L.append("No holdings yet.")
    b.tg("\n".join(L))


def main():
    if os.path.exists(STATE):
        s = json.load(open(STATE))
        if s.get("schema") != SCHEMA:
            s = None
    else:
        s = None
    if s is None:
        s, sc = seed(download("2y"))
        json.dump(s, open(STATE, "w"), indent=1)
        b.tg(f"Quarterly momentum tracker started (paper, ${START:,.0f}). History through {s['last_date']}. First picks "
             "(bought at the next US close): " + ", ".join(f"{k} ({v:+.0f}%)" for k, v in sc.items()))
        return
    data = download("1mo")
    new = sorted({r[0] for rows in data.values() for r in rows if r[0] > s["last_date"]})
    if not new:
        print("No new session.")
        return
    for d in new:
        for sym, rows in data.items():
            row = next((r for r in rows if r[0] == d), None)
            if row:
                s["hist"].setdefault(sym, []).append(list(row))
                s["hist"][sym] = s["hist"][sym][-KEEP:]
        process_day(s, d, b.tg)
        s["last_date"] = d
    digest(s, new[-1])
    json.dump(s, open(STATE, "w"), indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        b.tg("Tokenized-stock tracker problem:\n" + traceback.format_exc()[-1200:])
        raise
