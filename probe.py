"""One-off probe: what do NGX's ticker and chart-history endpoints return?"""
import json, urllib.request, urllib.parse, traceback
UA = {"User-Agent": "Mozilla/5.0 (compatible; ngx-paper-bot; educational)"}
B = "https://doclib.ngxgroup.com/REST/api/"
rep = {}

def get(url):
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40)
    return json.load(r)

def shape(j, n=2):
    if isinstance(j, list):
        return {"type": "list", "len": len(j), "head": j[:n], "tail": j[-n:]}
    if isinstance(j, dict):
        return {"type": "dict", "keys": list(j)[:15],
                "sub": {k: shape(v, n) for k, v in list(j.items())[:6] if isinstance(v, (list, dict))}}
    return {"type": str(type(j)), "val": str(j)[:100]}

try:
    u = B + "statistics/ticker?$filter=" + urllib.parse.quote("TickerType eq 'EQUITIES'") + "&page_size=1000"
    t = get(u)
    rep["ticker"] = shape(t, 2)
    rows = t if isinstance(t, list) else next((v for v in t.values() if isinstance(v, list)), [])
    seplat = next((r for r in rows if "SEPLAT" in json.dumps(r).upper()), None)
    rep["seplat_row"] = seplat
    ids = []
    if isinstance(seplat, dict):
        for k, v in seplat.items():
            if "id" in k.lower() and v not in (None, ""):
                ids.append((k, v))
    rep["id_candidates"] = ids
    rep["chart_tries"] = {}
    for k, v in ids[:4]:
        try:
            rep["chart_tries"][f"{k}={v}"] = shape(get(B + f"stockchartdata/{v}"), 3)
        except Exception as e:
            rep["chart_tries"][f"{k}={v}"] = f"ERR {e}"
except Exception:
    rep["error"] = traceback.format_exc()[-800:]
json.dump(rep, open("probe_report.json", "w"), indent=1, default=str)
print("done")
