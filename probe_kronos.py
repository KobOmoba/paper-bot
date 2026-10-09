"""One-off probe: can Kronos-small load and forecast on a GitHub Actions CPU runner, how long does it take, what is the exact API?"""
import json, time, sys, inspect, traceback
out = {"started": time.strftime("%F %T")}
try:
    sys.path.insert(0, "Kronos_repo")
    import torch
    out["torch"] = torch.__version__; out["cuda"] = torch.cuda.is_available()
    from model import Kronos, KronosTokenizer, KronosPredictor
    out["predict_sig"] = str(inspect.signature(KronosPredictor.predict))
    out["predict_batch_sig"] = str(inspect.signature(KronosPredictor.predict_batch)) if hasattr(KronosPredictor, "predict_batch") else None
    out["init_sig"] = str(inspect.signature(KronosPredictor.__init__))
    t = time.time()
    tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base"); mdl = Kronos.from_pretrained("NeoQuasar/Kronos-small")
    out["load_s"] = round(time.time() - t, 1)
    pr = KronosPredictor(mdl, tok, max_context=512)
    import yfinance as yf, pandas as pd
    px = yf.download(["BTC-USD", "SPY", "GLD"], period="3y", auto_adjust=True, progress=False)
    dfs, xts, yts = [], [], []
    for t_ in ["BTC-USD", "SPY", "GLD"]:
        d = pd.DataFrame({c.lower(): px[c][t_] for c in ["Open", "High", "Low", "Close", "Volume"]}).dropna().iloc[-512:]
        d["amount"] = d["volume"] * d["close"]
        idx = pd.Series(d.index)
        fut = pd.Series(pd.bdate_range(d.index[-1] + pd.Timedelta(days=1), periods=5)) if t_ != "BTC-USD" else pd.Series(pd.date_range(d.index[-1] + pd.Timedelta(days=1), periods=5))
        dfs.append(d.reset_index(drop=True)); xts.append(idx); yts.append(fut)
    out["bars"] = [len(d) for d in dfs]
    t = time.time()
    res = pr.predict_batch(df_list=dfs, x_timestamp_list=xts, y_timestamp_list=yts, pred_len=5, T=1.0, top_p=0.9, sample_count=1, verbose=False)
    out["batch_s"] = round(time.time() - t, 1)
    out["batch_cols"] = list(res[0].columns); out["batch_first_rows"] = res[0].head(2).round(4).to_dict("records")
    out["last_close_btc"] = float(dfs[0]["close"].iloc[-1])
    t = time.time()
    r2 = pr.predict(df=dfs[1], x_timestamp=xts[1], y_timestamp=yts[1], pred_len=5, T=1.0, top_p=0.9, sample_count=1, verbose=False)
    out["single_s"] = round(time.time() - t, 1); out["single_rows"] = len(r2)
    out["ok"] = True
except Exception as e:
    out["ok"] = False; out["error"] = repr(e); out["tb"] = traceback.format_exc()[-1500:]
json.dump(out, open("probe_kronos.json", "w"), indent=1); print(json.dumps(out, indent=1))
