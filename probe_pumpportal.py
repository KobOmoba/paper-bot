"""One-off probe: can GitHub Actions reach PumpPortal's free data websocket, and what do events look like?
Writes probe_pumpportal.json (committed by the workflow). Read-only, no keys, no trading."""
import asyncio, json, time
import websockets

URL = "wss://pumpportal.fun/api/data"
out = {"url": URL, "connected": False, "error": None, "events": 0, "types": {}, "sample_keys": None, "sample": None,
       "first_event_after_s": None, "trade_events": 0, "seconds": 45}


async def main():
    t0 = time.time()
    try:
        async with websockets.connect(URL, open_timeout=15) as ws:
            out["connected"] = True
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            mints = []
            while time.time() - t0 < out["seconds"]:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                try:
                    d = json.loads(msg)
                except Exception:
                    continue
                out["events"] += 1
                if out["first_event_after_s"] is None:
                    out["first_event_after_s"] = round(time.time() - t0, 1)
                k = str(d.get("txType") or d.get("message") or "other")
                out["types"][k] = out["types"].get(k, 0) + 1
                if out["sample"] is None and d.get("mint"):
                    out["sample_keys"], out["sample"] = sorted(d.keys()), {x: d[x] for x in d if x != "uri"}
                if d.get("txType") == "create" and d.get("mint") and len(mints) < 5:
                    mints.append(d["mint"])
                    await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [d["mint"]]}))
                if d.get("txType") in ("buy", "sell"):
                    out["trade_events"] += 1
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out["elapsed_s"] = round(time.time() - t0, 1)


asyncio.run(main())
json.dump(out, open("probe_pumpportal.json", "w"), indent=1)
print(json.dumps(out, indent=1)[:1500])
