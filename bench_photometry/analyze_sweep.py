#!/usr/bin/env python3
"""Reduce a sweep.py CSV: light as a fraction of level 254, against the
firmware's expected PWM duty (expected.py), with the standard deviation next to
every mean. Ambient (light off) is subtracted. Both sensors, both directions.

  analyze_sweep.py results/A_b40_1x.csv [--fw b40|b36] [--mireds 230]
"""
import argparse, csv, statistics, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expected import expected, PWM_MAX_TICK

ap = argparse.ArgumentParser()
ap.add_argument("csv")
ap.add_argument("--fw", default="b40")
ap.add_argument("--mireds", type=int, default=230)
a = ap.parse_args()

rows = [r for r in csv.DictReader(open(a.csv))]
amb = [r for r in rows if r["phase"].startswith("ambient")]
amb_bh = statistics.mean(float(r["bh_mean"]) for r in amb) if amb else 0.0
amb_c0 = statistics.mean(float(r["c0_mean"]) for r in amb) if amb else 0.0
lit = [r for r in rows if not r["phase"].startswith("ambient")]
if not lit:
    sys.exit("no lit rows")

def duty(level):
    c36, w36, c40, w40 = expected(level, a.mireds)
    s = (c40 + w40) if a.fw == "b40" else (c36 + w36)
    return s / (2 * PWM_MAX_TICK)          # two channels, each of PWM_MAX_TICK

full = [r for r in lit if int(r["level"]) == 254]
full_bh = statistics.mean(float(r["bh_mean"]) for r in full) - amb_bh
full_c0 = statistics.mean(float(r["c0_mean"]) for r in full) - amb_c0
full_duty = duty(254)
print(f"ambient BH {amb_bh:.1f}  ch0 {amb_c0:.1f}   |  level 254: BH {full_bh:.0f}  ch0 {full_c0:.0f}   duty {100*full_duty:.2f}%")
print(f"{'lvl':>4} {'dir':>4} {'conf':>4} {'duty%':>6} {'dutyRel%':>8} | {'BH%':>7} {'±%':>6} | {'ch0%':>7} {'±%':>6} | {'light/duty':>10} {'note'}")
for r in sorted(lit, key=lambda r: (int(r["level"]), r["phase"])):
    L = int(r["level"]); d = duty(L); drel = d / full_duty
    bh = float(r["bh_mean"]) - amb_bh; bsd = float(r["bh_sd"])
    c0 = float(r["c0_mean"]) - amb_c0; csd = float(r["c0_sd"])
    bhp = 100 * bh / full_bh; c0p = 100 * c0 / full_c0
    note = ""
    if bh < 3 * max(bsd, 1): note = "DARK"
    elif r["sat"]: note = "SAT " + r["sat"]
    print(f"{L:>4} {r['phase']:>4} {r['confirm']:>4} {100*d:6.2f} {100*drel:8.2f} | {bhp:7.2f} {100*bsd/full_bh:6.3f} | {c0p:7.2f} {100*csd/full_c0:6.3f} | {(bhp/(100*drel)) if drel else 0:10.2f} {note}")
