#!/usr/bin/env python3
"""Reduce a fade.py CSV. Folds repeated reads of one integration, expresses
light as a fraction of the pre-fade level, adds the cube-root (perceived)
fraction, and overlays what build 40's cubic pacing intends.

  analyze_fade.py results/D_8s.csv [--curve results/A_b40_1x.csv] [--step 0.5]
With --curve, the measured level->light table from a sweep is used to turn the
intended level(t) into an intended light(t); without it, light is assumed
proportional to duty (the brief's assumption).
"""
import argparse, csv, statistics, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected import expected, PWM_MAX_TICK

ap = argparse.ArgumentParser()
ap.add_argument("csv")
ap.add_argument("--curve")
ap.add_argument("--step", type=float, default=0.5)
ap.add_argument("--tail", type=float, default=2.0, help="seconds before the end shown at fine resolution")
ap.add_argument("--fine", type=float, default=0.06)
a = ap.parse_args()

lines = list(csv.reader(open(a.csv)))
hdr = lines[0]; meta = dict(zip(hdr[0::2], hdr[1::2]))
seconds = float(meta["seconds"]); frm = meta["from"]; to = meta["to"]
rows = [(float(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in lines[2:]]

# fold: keep a BH sample only when its value changes or 0.05 s passed (one integration ~54 ms)
bh = []; last = None; lt = -9
for t, b, c0, c1 in rows:
    if b != last or t - lt >= 0.05:
        bh.append((t, b)); last = b; lt = t
pre = [b for t, b in bh if t < -0.2]
base = statistics.mean(pre) if pre else float("nan")
amb = min(b for t, b in bh)                       # darkest sample = ambient floor
def frac(v): return max(0.0, (v - amb) / (base - amb)) if base > amb else 0.0

start_level = 254 if frm == "off" else int(frm)
target = 0 if to == "off" else int(to)
def duty(level):
    if level <= 0: return 0.0
    c36, w36, c40, w40 = expected(int(round(level)), 230)
    return (c40 + w40) / (2 * PWM_MAX_TICK)

curve = None
if a.curve:
    curve = {}
    for r in csv.DictReader(open(a.curve)):
        if r["phase"] in ("up", "down") and r["confirm"] == "z2m":
            curve.setdefault(int(r["level"]), []).append(float(r["bh_mean"]))
    camb = statistics.mean(float(r["bh_mean"]) for r in csv.DictReader(open(a.curve)) if r["phase"].startswith("ambient"))
    cfull = statistics.mean(curve[254]) - camb
    curve = {L: (statistics.mean(v) - camb) / cfull for L, v in curve.items()}
    ks = sorted(curve)
    def light_of_level(x):
        if x <= 0: return 0.0
        if x <= ks[0]: return curve[ks[0]] * x / ks[0]
        for i in range(1, len(ks)):
            if x <= ks[i]:
                x0, x1 = ks[i-1], ks[i]; y0, y1 = curve[x0], curve[x1]
                return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
        return curve[ks[-1]]
else:
    d254 = duty(254)
    def light_of_level(x): return duty(x) / d254 if x > 0 else 0.0

def intended_level(t):
    """tuyaLight_levelPaced: level = target + span * x^3, x = remaining/total."""
    if t < 0: return start_level
    if t >= seconds: return target
    x = (seconds - t) / seconds
    return target + (start_level - target) * x ** 3

print(f"# {a.csv}: {frm} -> {to} over {seconds}s; pre-fade BH {base:.0f}, ambient {amb}; {len(bh)} folded samples")
print(f"{'t(s)':>6} {'BH':>7} {'light%':>7} {'cbrt%':>6} | {'intended lvl':>12} {'intended light%':>15} {'cbrt%':>6}")
marks = [i * a.step for i in range(int(seconds / a.step) + 1)]
tail_start = seconds - a.tail
marks = [m for m in marks if m < tail_start] + [tail_start + i * a.fine for i in range(int((a.tail + 0.6) / a.fine) + 1)]
for m in marks:
    near = [b for t, b in bh if abs(t - m) <= max(a.fine / 2, 0.03)]
    if not near: continue
    v = statistics.mean(near); f = frac(v)
    il = intended_level(m); ilight = light_of_level(il)
    print(f"{m:6.2f} {v:7.0f} {100*f:7.2f} {100*f**(1/3):6.1f} | {il:12.2f} {100*ilight:15.2f} {100*ilight**(1/3):6.1f}")
# the final step
lit = [(t, b) for t, b in bh if t > 0 and frac(b) > 0.005]
if lit:
    t_last, b_last = lit[-1]
    dark = [(t, b) for t, b in bh if t > t_last and frac(b) <= 0.005]
    t_dark = dark[0][0] if dark else float("nan")
    print(f"# last lit sample t={t_last:.3f}s at {100*frac(b_last):.2f}% light ({100*frac(b_last)**(1/3):.1f}% perceived); dark by t={t_dark:.3f}s")
