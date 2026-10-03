"""Tokenized-stock MOMENTUM tracker (PAPER ONLY). Tokenized stocks (e.g. Binance bStocks, Robinhood Stock Tokens)
track US stock prices, so we run the momentum strategy on the underlying stocks and add a conservative cost
assumption for tokens (0.5% fee + 1% slippage per side). No orders are ever placed. Runs after the US close."""
import json, os, sys, traceback
import yfinance as yf
import ngx_bot as b

TICKERS = """AAPL MSFT AMZN GOOGL NVDA ORCL INTC CSCO IBM QCOM TXN ADBE AMGN GILD JNJ PFE MRK ABT LLY UNH CVS MDT BMY
JPM BAC WFC C GS MS AXP USB PNC COF KO PEP PG WMT COST MCD SBUX NKE DIS CMCSA T VZ XOM CVX COP SLB OXY EOG HAL
CAT DE BA HON GE MMM UPS FDX LMT RTX NOC GD UNP CSX NSC HD LOW TGT TJX F DUK SO NEE D EXC AEP MO PM CL KMB
MCK CI ISRG BIIB MU AMAT ADI TSLA META NFLX AMD AVGO CRM PYPL UBER""".split()
STATE, NAME = "us_state.json", "momentum"
b.START, b.MIN_AVG_VALUE, b.FEE, b.SLIP = 100_000.0, 20_000_000, 0.005, 0.01
b.SKIP_LIMIT_UP, b.MAX_POS, b.POS_FRAC = 1e9, 10, 0.10
b.CUR = "$"


def download(period):
    df = yf.download(TICKERS, period=period, auto_adjust=True, group_by="ticker", progress=False, threads=True)
    out = {}
    for t in TICKERS:
        try:
            d = df[t].dropna(subset=["Close"])
        except Exception:
            continue
        out[t] = [(i.strftime("%Y-%m-%d"), float(r["Open"]), float(r["Close"]), float(r["Volume"] or 0))
                  for i, r in d.iterrows()]
    return out


def seed(data):
    s = {"hist": {}, "last_date": "", "accts": {NAME: b.new_acct()}}
    for sym, rows in data.items():
        h, prev = [], None
        for d, o, c, v in rows:
            h.append([d, c, v, round((c / prev - 1) * 100, 2) if prev else 0.0])
            prev = c
        s["hist"][sym] = h[-b.HIST_KEEP:]
    s["last_date"] = max(r[-1][0] for r in s["hist"].values() if r)
    return s


def by_date(data):
    by = {}
    for sym, rows in data.items():
        for i, (d, o, c, v) in enumerate(rows):
            by.setdefault(d, {})[sym] = {"open": o, "close": c, "vol": v, "prev": rows[i - 1][2] if i else c}
    return by


def digest(s, date):
    a = s["accts"][NAME]
    ctx = b.market_ctx(s["hist"])
    top = sorted(ctx["rank"], key=ctx["rank"].get, reverse=True)[:10]
    eq = b.equity(a)
    L = [f"Tokenized-stock momentum (paper) {date}",
         f"Equity ${eq:,.0f} ({(eq / b.START - 1) * 100:+.1f}%) | cash ${a['cash']:,.0f} | open {len(a['pos'])} | closed {len(a['closed'])}",
         f"Market breadth: {ctx['breadth'] * 100:.0f}% of liquid stocks above 50-day average "
         f"({'healthy' if ctx['breadth'] >= 0.5 else 'weak'})",
         "Strongest 60-day performers: " + ", ".join(top)]
    for p in a["pos"]:
        L.append(f"  {p['sym']} {(p['mark'] / p['entry'] - 1) * 100:+.1f}% ({p['days']}d)")
    if a["pending"]:
        L.append("Queued for next open: " + ", ".join(f"{o['side']} {o['sym']}" for o in a["pending"]))
    b.tg("\n".join(L))


def main():
    if not os.path.exists(STATE):
        s = seed(download("8mo"))
        json.dump(s, open(STATE, "w"), indent=1)
        b.tg(f"Tokenized-stock tracker started: history loaded through {s['last_date']} ({len(s['hist'])} stocks). "
             "No trades yet; signals begin with the next US session.")
        return
    s = json.load(open(STATE))
    by = by_date(download("1mo"))
    new = sorted(d for d in by if d > s["last_date"])
    if not new:
        print("No new session.")
        return
    for d in new:
        b.process_day(s, by[d], d, say=b.tg)
    digest(s, new[-1])
    json.dump(s, open(STATE, "w"), indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        b.tg("Tokenized-stock tracker problem:\n" + traceback.format_exc()[-1200:])
        raise
