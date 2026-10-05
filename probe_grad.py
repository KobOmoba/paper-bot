"""One-off probe: does PumpPortal's free websocket send graduation (migration) events, and what do they contain?
Read-only, no keys, no trading. Writes probe_grad.json."""
import asyncio, json, time
import websockets

URL = "wss://pumpportal.fun/api/data"
SECONDS = 240
out = {"connected": False, "error": None, "events": 0, "types": {}, "samples": [], "seconds": SECONDS}


async def main():
    t0 = time.time()
    try:
        async with websockets.connect(URL, open_timeout=15, ping_interval=20) as ws:
            out["connected"] = True
            await ws.send(json.dumps({"method": "subscribeMigration"}))
            while time.time() - t0 < SECONDS:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                try:
                    d = json.loads(msg)
                except Exception:
                    continue
                out["events"] += 1
                k = str(d.get("txType") or d.get("message") or "other")
                out["types"][k] = out["types"].get(k, 0) + 1
                if len(out["samples"]) < 6:
                    out["samples"].append({"t": round(time.time() - t0, 1), "d": {x: d[x] for x in d if x != "uri"}})
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out["elapsed_s"] = round(time.time() - t0, 1)


asyncio.run(main())
json.dump(out, open("probe_grad.json", "w"), indent=1)
print(json.dumps(out, indent=1)[:2500])
