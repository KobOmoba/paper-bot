"""One-off probe: can we price a Pump.fun token straight from its bonding-curve account (no DexScreener)?
For fresh launches from PumpPortal, read the curve account over public Solana RPC, decode it with the layout
[8 discriminator][u64 virtualTokenReserves][u64 virtualSolReserves][u64 realTokenReserves][u64 realSolReserves][u64 supply][bool complete]
and compare with the numbers in the creation event. Writes probe_curve.json. Read-only; no keys."""
import asyncio, base64, json, struct, time, urllib.request
import websockets

RPC = "https://api.mainnet-beta.solana.com"
out = {"events": 0, "checked": [], "error": None}


def rpc(method, params):
    req = urllib.request.Request(RPC, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "probe"})
    return json.load(urllib.request.urlopen(req, timeout=20))


async def main():
    t0, picked = time.time(), []
    try:
        async with websockets.connect("wss://pumpportal.fun/api/data", open_timeout=15) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            while time.time() - t0 < 25 and len(picked) < 6:
                try:
                    d = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                except asyncio.TimeoutError:
                    continue
                if d.get("txType") == "create" and d.get("bondingCurveKey"):
                    out["events"] += 1
                    picked.append((time.time(), d))
    except Exception as e:
        out["error"] = f"ws {type(e).__name__}: {str(e)[:150]}"
    time.sleep(3)
    for t_recv, d in picked:
        row = {"mint": d["mint"], "event_vSol": d["vSolInBondingCurve"], "event_vTok": d["vTokensInBondingCurve"]}
        try:
            t1 = time.time()
            r = rpc("getAccountInfo", [d["bondingCurveKey"], {"encoding": "base64", "commitment": "confirmed"}])
            row["rpc_s"] = round(time.time() - t1, 2)
            v = (r.get("result") or {}).get("value")
            if not v:
                row["note"] = "account not found yet: " + json.dumps(r)[:150]
            else:
                raw = base64.b64decode(v["data"][0])
                vt, vs, rt, rs, sup = struct.unpack_from("<5Q", raw, 8)
                row.update({"len": len(raw), "vTok_decoded": vt / 1e6, "vSol_decoded": vs / 1e9, "realSol": rs / 1e9,
                            "complete": bool(raw[48]) if len(raw) > 48 else None,
                            "price_sol_decoded": (vs / 1e9) / (vt / 1e6), "price_sol_event": d["vSolInBondingCurve"] / d["vTokensInBondingCurve"]})
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        out["checked"].append(row)
    # batch call: getMultipleAccounts for all picked curves in one request
    try:
        r = rpc("getMultipleAccounts", [[d["bondingCurveKey"] for _, d in picked], {"encoding": "base64"}])
        out["multi_ok"] = isinstance((r.get("result") or {}).get("value"), list)
    except Exception as e:
        out["multi_ok"] = f"{type(e).__name__}: {str(e)[:100]}"


asyncio.run(main())
json.dump(out, open("probe_curve.json", "w"), indent=1)
print(json.dumps(out, indent=1)[:3000])
