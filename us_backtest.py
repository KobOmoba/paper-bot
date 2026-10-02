"""Same six paper strategies, tested on ~85 large US stocks (NYSE/NASDAQ) using free Yahoo data.
Costs: ~0.05% fee + 0.05% slippage per side (large, liquid stocks, commission-free brokers).
CAVEAT: stocks are today's large caps, so results are flattered (survivorship bias)."""
import json, sys
import yfinance as yf
import ngx_bot as b

TICKERS = """AAPL MSFT AMZN GOOGL NVDA ORCL INTC CSCO IBM QCOM TXN ADBE AMGN GILD JNJ PFE MRK ABT LLY UNH CVS MDT BMY
JPM BAC WFC C GS MS AXP USB PNC COF KO PEP PG WMT COST MCD SBUX NKE DIS CMCSA T VZ XOM CVX COP SLB OXY EOG HAL
CAT DE BA HON GE MMM UPS FDX LMT RTX NOC GD UNP CSX NSC HD LOW TGT TJX F DUK SO NEE D EXC AEP MO PM CL KMB
MCK CI ISRG BIIB MU AMAT ADI""".split()
START = "2005-01-01"

data = yf.download(TICKERS + ["SPY", "QQQ"], start=START, auto_adjust=True, group_by="ticker",
                   progress=False, threads=True)
hist, bench = {}, {}
for t in TICKERS + ["SPY", "QQQ"]:
    try:
        df = data[t].dropna(subset=["Close"])
    except Exception:
        continue
    rows, prev = [], None
    for dt, r in df.iterrows():
        c, v = float(r["Close"]), float(r["Volume"] or 0)
        rows.append([dt.strftime("%Y-%m-%d"), round(c, 4), v, round((c / prev - 1) * 100, 2) if prev else 0.0])
        prev = c
    (bench if t in ("SPY", "QQQ") else hist)[t] = rows
print("stocks", len(hist), "bench", {k: len(v) for k, v in bench.items()})
json.dump(hist, open("us_history.json", "w"), separators=(",", ":"))

b.START, b.MIN_AVG_VALUE, b.FEE, b.SLIP = 100_000.0, 20_000_000, 0.0005, 0.0005
b.SKIP_LIMIT_UP, b.HIST_KEEP, b.POS_FRAC, b.MAX_POS = 1e9, 120, 0.10, 5
b.tg = lambda t: print(t)           # capture summary text, send once at the end
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    b.backtest(histfile="us_history.json", outfile="us_backtest_report.json", label="US large-cap")
rep = json.load(open("us_backtest_report.json"))
for k, rows in bench.items():
    for p, (lo, hi) in rep["periods"].items():
        w = [r[1] for r in rows if lo <= r[0] <= hi]
        rep.setdefault("index_buyhold_pct", {}).setdefault(k, {})[p] = round((w[-1] / w[0] - 1) * 100, 1) if len(w) > 1 else None
json.dump(rep, open("us_backtest_report.json", "w"), indent=1)
msg = buf.getvalue() + "\nIndex buy & hold: " + json.dumps(rep["index_buyhold_pct"])
print(msg)
import os, urllib.parse, urllib.request
if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
    d = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": msg[:4000]}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage", data=d), timeout=20)
