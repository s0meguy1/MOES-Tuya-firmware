#!/usr/bin/env python3
"""Expected PWM compare ticks per ZCL level, both firmware generations, at a
given colour temperature. Exact integer replicas of:
  build 34..37  moes_levelCurve / moes_duty / pwmSetDuty / temperatureToCW   (u8 chain)
  build 38..40  moes_dimSplitCW256 / moes_dimCurve256 / moes_dimCmpTick       (8.8 chain)
Checked against tools/level_curve_hosttest: level 1 -> C 156, level 254 -> C 9327 at 230 mireds.

  expected.py [--mireds 230] [--csv out.csv] [--levels 1-8,16,...]
"""
import argparse, csv, sys

PWM_MAX_TICK = 12000
CT_MIN, CT_MAX = 153, 500
BMIN, BMAX = 1, 100
MAX256 = 255 * 256


def legacy_curve(v):                      # moes_levelCurve, u8
    if v == 0:
        return 0
    pct = BMIN + (v * (BMAX - BMIN)) // 255
    return (pct * 255) // 100


def legacy_tick(v):                       # moes_duty * pwmSetDuty
    return ((legacy_curve(v) * 100) * PWM_MAX_TICK) // (254 * 100)


def legacy_split(level, mireds):          # temperatureToCW, u8
    W = ((mireds - CT_MIN) * level) // (CT_MAX - CT_MIN)
    return level - W, W


def curve256(v):                          # moes_dimCurve256
    if v == 0:
        return 0
    v = min(v, MAX256)
    return (BMIN * MAX256 + (BMAX - BMIN) * v) // BMAX


def tick256(v):                           # moes_dimCmpTick
    return (min(v, MAX256) * PWM_MAX_TICK) // MAX256


def split256(level256, mireds):           # moes_dimSplitCW256
    W = ((mireds - CT_MIN) * level256) // (CT_MAX - CT_MIN)
    return level256 - W, W


def expected(level, mireds):
    C, W = legacy_split(level, mireds)
    c36, w36 = legacy_tick(C), legacy_tick(W)
    C256, W256 = split256(level << 8, mireds)
    c40, w40 = tick256(curve256(C256)), tick256(curve256(W256))
    return c36, w36, c40, w40


def parse_levels(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-"); out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mireds", type=int, default=230)
    ap.add_argument("--csv")
    ap.add_argument("--levels", default="1-254")
    a = ap.parse_args()
    rows = []
    for L in parse_levels(a.levels):
        c36, w36, c40, w40 = expected(L, a.mireds)
        rows.append((L, c36, w36, c36 + w36, c40, w40, c40 + w40))
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["level", "b36_C", "b36_W", "b36_sum", "b40_C", "b40_W", "b40_sum"])
            w.writerows(rows)
    print(f"mireds={a.mireds}   ticks of {PWM_MAX_TICK}")
    print(f"{'lvl':>4} {'b36 C':>6} {'b36 W':>6} {'sum':>6} | {'b40 C':>6} {'b40 W':>6} {'sum':>6}  b40 sum %")
    for r in rows:
        print(f"{r[0]:>4} {r[1]:>6} {r[2]:>6} {r[3]:>6} | {r[4]:>6} {r[5]:>6} {r[6]:>6}  {100*r[6]/rows[-1][6]:6.2f}")
