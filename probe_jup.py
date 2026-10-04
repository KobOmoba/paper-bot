"""One-off probe (paper only, no keys): can Actions reach Jupiter's quote API, and does a sell quote exist for fresh Solana tokens?
Writes probe_jup.json. A quote is only a proxy for 'simulate sell' (it does not catch every honeypot)."""
import json, time, urllib.request, urllib.parse, base64
SOL = "So11111111111111111111111111111111111111112"
out = {"quote_host": None, "tokens": [], "errors": []}
def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 probe", "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))
def rpc(method, params):
    req = urllib.request.Request("https://api.mainnet-beta.solana.com", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(), headers={"Content-Type": "application/json", "User-Agent": "probe"})
    return json.load(urllib.request.urlopen(req, timeout=20))
try:
    prof = get("https://api.dexscreener.com/token-profiles/latest/v1")
    mints = [p["tokenAddress"] for p in prof if p.get("chainId") == "solana"][:12]
except Exception as e:
    mints = []; out["errors"].append("profiles " + repr(e)[:120])
pairs = {}
for i in range(0, len(mints), 30):
    try:
        for p in get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i+30])):
            a = p["baseToken"]["address"]; liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if a not in pairs or liq > pairs[a][1]: pairs[a] = (p, liq)
    except Exception as e:
        out["errors"].append("pairs " + repr(e)[:120])
for m, (p, liq) in list(pairs.items())[:10]:
    row = {"mint": m, "sym": p["baseToken"].get("symbol"), "liq_usd": liq}
    try:
        ai = rpc("getAccountInfo", [m, {"encoding": "base64"}])["result"]["value"]
        dec = base64.b64decode(ai["data"][0])[44]
        price = float(p["priceUsd"]); pn = float(p["priceNative"])
        tokens = 20.0 / price                              # about $20 of the token
        amount = int(tokens * 10 ** dec)
        row.update({"decimals": dec, "expected_sol": tokens * pn})
        for host in ("https://lite-api.jup.ag/swap/v1/quote", "https://api.jup.ag/swap/v1/quote"):
            try:
                q = get(host + "?" + urllib.parse.urlencode({"inputMint": m, "outputMint": SOL, "amount": amount, "slippageBps": 1000}), 15)
                out["quote_host"] = host
                row.update({"quote_ok": True, "out_sol": int(q["outAmount"]) / 1e9, "price_impact_pct": q.get("priceImpactPct"), "routes": len(q.get("routePlan") or [])})
                row["ratio"] = row["out_sol"] / row["expected_sol"] if row["expected_sol"] else None
                break
            except urllib.error.HTTPError as e:
                row["quote_ok"] = False; row["err_" + host.split("/")[2]] = f"{e.code} {e.read()[:120]!r}"
            except Exception as e:
                row["quote_ok"] = False; row["err_" + host.split("/")[2]] = repr(e)[:120]
    except Exception as e:
        row["error"] = repr(e)[:150]
    out["tokens"].append(row); time.sleep(1.2)
json.dump(out, open("probe_jup.json", "w"), indent=1)
print(json.dumps(out, indent=1)[:3500])
