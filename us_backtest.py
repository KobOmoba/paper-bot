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
import os as _os
DATASET = _os.environ.get("DATASET", "us")
if DATASET == "crypto":
    TICKERS = """BTC-USD ETH-USD BNB-USD XRP-USD ADA-USD DOGE-USD SOL-USD DOT-USD LTC-USD LINK-USD BCH-USD XLM-USD
TRX-USD ETC-USD XMR-USD EOS-USD ATOM-USD VET-USD XTZ-USD NEO-USD DASH-USD ZEC-USD ALGO-USD AVAX-USD SHIB-USD
FIL-USD ICP-USD NEAR-USD MANA-USD SAND-USD THETA-USD EGLD-USD HBAR-USD ENJ-USD BAT-USD KSM-USD CHZ-USD CRV-USD
COMP-USD MKR-USD SNX-USD YFI-USD SUSHI-USD AAVE-USD FLOKI-USD PEPE24478-USD BONK-USD""".split()
    BENCH = ["BTC-USD", "ETH-USD"]
    START = "2018-01-01"
elif DATASET == "small":
    TICKERS = """AAON ABCB ACIW AEO AGYS ALG AMN ANF ARCB ASTE ATI ATKR AVAV BCC BCPC BKE BLMN BMI BOOT BRC CAKE CALM
CBRL CCS CNS CPK CRVL CVCO CWT DIOD DORM EXPO FELE FIZZ FORM FOXF GBX GFF GPI HNI HWKN IIIN INDB JBT KAI KFRC KTB
LCII LGND LZB MATX MGEE MLI MMSI MOG-A MTH NHC NSIT OSIS PATK PLXS POWL PRGS RUSHA SHOO SIGI SKYW SLP SM SPSC STRA
SXI TNC UFPT UFPI VIRT WDFC WHD WIRE YELP""".split()
    BENCH = ["IWM", "IJR"]
    START = "2005-01-01"
else:
    BENCH = ["SPY", "QQQ"]
    START = "2005-01-01"

data = yf.download(TICKERS + BENCH, start=START, auto_adjust=True, group_by="ticker",
                   progress=False, threads=True)
hist, bench = {}, {}
for t in TICKERS + BENCH:
    try:
        df = data[t].dropna(subset=["Close"])
    except Exception:
        continue
    rows, prev = [], None
    for dt, r in df.iterrows():
        c, v = float(r["Close"]), float(r["Volume"] or 0)
        rows.append([dt.strftime("%Y-%m-%d"), round(c, 4), v, round((c / prev - 1) * 100, 2) if prev else 0.0])
        prev = c
    (bench if t in BENCH else hist)[t] = rows
print("stocks", len(hist), "bench", {k: len(v) for k, v in bench.items()})
json.dump(hist, open(f"{DATASET}_history.json", "w"), separators=(",", ":"))

b.START, b.MIN_AVG_VALUE, b.FEE, b.SLIP = 100_000.0, 20_000_000, 0.0005, 0.0005
if DATASET == "crypto":
    b.MIN_AVG_VALUE, b.FEE, b.SLIP = 1_000_000, 0.001, 0.003
elif DATASET == "small":
    b.MIN_AVG_VALUE, b.FEE, b.SLIP = 5_000_000, 0.001, 0.002
b.SKIP_LIMIT_UP, b.HIST_KEEP, b.POS_FRAC, b.MAX_POS = 1e9, 120, 0.10, 5
import os
FULL = os.environ.get("FULL") == "true"
if FULL:
    b.MAX_POS = 10          # pre-registered change: 10 x 10% = fully invested (was 5 x 10% = half)
OUT = f"{DATASET}_backtest_full_report.json" if FULL else f"{DATASET}_backtest_report.json"
LABEL = DATASET + (" fully-invested" if FULL else "")
b.tg = lambda t: print(t)           # capture summary text, send once at the end
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    b.backtest(histfile=f"{DATASET}_history.json", outfile=OUT, label=LABEL)
rep = json.load(open(OUT))
for k, rows in bench.items():
    for p, (lo, hi) in rep["periods"].items():
        w = [r[1] for r in rows if lo <= r[0] <= hi]
        rep.setdefault("index_buyhold_pct", {}).setdefault(k, {})[p] = round((w[-1] / w[0] - 1) * 100, 1) if len(w) > 1 else None
json.dump(rep, open(OUT, "w"), indent=1)
msg = buf.getvalue() + "\nIndex buy & hold: " + json.dumps(rep["index_buyhold_pct"])
print(msg)
import os, urllib.parse, urllib.request
if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
    d = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": msg[:4000]}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}/sendMessage", data=d), timeout=20)
