"""Replay exit variants on recorded price paths of closed paper trades (paper only).
Path points: [ts, price, liq]. Paths end where the baseline exit happened, so variants that hold LONGER
than baseline can only be judged up to that point (remaining bag closed at last seen price) -> optimistic/blind for them."""
import json, sys, glob
STAKE, SLIP, FEE, RUGPAY = 5.0, 0.07, 0.01, 0.30

def load():
    out = []
    for f in ["meme_loose_state.json", "meme_accel_state.json", "meme_birth_state.json", "meme_state.json"]:
        try: s = json.load(open(f))
        except Exception: continue
        for t in s["closed"]:
            p = t.get("path") or t.get("ctx", {}).get("path") or []
            if len(p) >= 3: out.append((f, t, p))
    return out

def run(t, path, stop=-0.25, tiers=((2, .5), (4, .3)), trail=.12, deadline_h=0.75, ladder=None, post_stop=None):
    e = t["entry_obs"]; impact = STAKE / max(t["liq_entry"], 1.0)
    out = lambda frac, p: STAKE * frac * (p / e) * (1 - SLIP - impact) * (1 - FEE)
    left, got, hit, peak, t0 = 1.0, 0.0, 0, e, path[0][0]
    tiers = list(ladder or tiers)
    prev_p = e
    for ts, p, liq in path:
        m = p / e; peak = max(peak, p)
        if t.get("reason", "").startswith("rug") and (ts, p, liq) == tuple(path[-1]):
            got += STAKE * left * (p / e) * RUGPAY; return got / STAKE
        while hit < len(tiers) and m >= tiers[hit][0] and left > 0:
            f = min(tiers[hit][1] if tiers[hit][1] <= 1 else 1, left)
            got += out(f, p); left -= f; hit += 1; peak = p
        if left <= 1e-9: return got / STAKE
        if hit == 0 and m <= 1 + stop: return (got + out(left, p)) / STAKE
        if hit > 0 and trail and p <= peak * (1 - trail) and (hit >= len(tiers) or ladder): return (got + out(left, p)) / STAKE
        if hit > 0 and post_stop and m <= post_stop: return (got + out(left, p)) / STAKE
        if hit == 0 and deadline_h and (ts - t0) / 3600 >= deadline_h: return (got + out(left, p)) / STAKE
    return (got + out(left, path[-1][1])) / STAKE

V = {
 "baseline (-25% stop, 50%@2x, 30%@4x, 12% trail, 45min)": {},
 "trail 12% right after 2x (no stop gap)": {"post_stop": None, "tiers": ((2, .5), (4, .3))},
 "stop after 2x at breakeven (1.0x)": {"post_stop": 1.0},
 "early T1: sell 50%@1.5x": {"tiers": ((1.5, .5), (4, .3))},
 "tighter stop -15%": {"stop": -.15},
 "looser stop -40%": {"stop": -.40},
 "no stop at all": {"stop": -.99},
 "deadline 20min": {"deadline_h": 0.33},
 "deadline 90min": {"deadline_h": 1.5},
 "Ascending Shave (50%@10x,25x,100x,300x; 15% trail)": {"ladder": ((10, .5), (25, .5), (100, .5), (300, .5)), "trail": .15},
}

if __name__ == "__main__":
    D = load(); print(f"{len(D)} closed trades with paths; reached 2x on path: {sum(1 for _,t,p in D if max(x[1] for x in p)>=2*t['entry_obs'])}; 5x: {sum(1 for _,t,p in D if max(x[1] for x in p)>=5*t['entry_obs'])}; 10x: {sum(1 for _,t,p in D if max(x[1] for x in p)>=10*t['entry_obs'])}")
    for name, kw in V.items():
        r = [run(t, p, **kw) for _, t, p in D]
        r.sort()
        print(f"{name:58s} avg {sum(r)/len(r):.3f}x  median {r[len(r)//2]:.3f}x  win {100*sum(x>1 for x in r)/len(r):.1f}%")
