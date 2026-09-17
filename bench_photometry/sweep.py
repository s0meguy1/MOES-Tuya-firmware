#!/usr/bin/env python3
"""Tests A and C of AI_DROPOUT_BRIEF.md: static transfer curve, and
repeatability at the bottom. Set a level, confirm it landed, settle, take N
reads of both sensors, record mean and sd of RAW counts.

  sweep.py --out results.csv                       # test A, default level list, up then down
  sweep.py --out bottom.csv --levels 1,2,3,5,8,12 --reads 50 --settle 3 --tsl-gain 25x   # test C

Rows are written and flushed as they are taken, so a dead link loses nothing.
"""
import argparse
import csv
import sys
import time

from photolib import Fixture, Sensors, parse_levels, stats, BH_SAT

DEFAULT_LEVELS = "1-16,18,20,24,28,32,40,48,56,64,80,96,112,128,144,160,176,192,208,224,240,254"
COLS = ["ts", "phase", "level", "confirm", "ack_s", "attempts", "n",
        "bh_mean", "bh_sd", "bh_min", "bh_max",
        "c0_mean", "c0_sd", "c0_min", "c0_max",
        "c1_mean", "c1_sd", "c1_min", "c1_max", "sat", "z2m_brightness"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="moes_bench")
    ap.add_argument("--levels", default=DEFAULT_LEVELS)
    ap.add_argument("--reads", type=int, default=10)
    ap.add_argument("--settle", type=float, default=1.5, help="seconds after the ack before reading")
    ap.add_argument("--dirs", default="up,down", help="up, down, or up,down")
    ap.add_argument("--tsl-gain", default="1x")
    ap.add_argument("--tsl-atime", type=int, default=100)
    ap.add_argument("--bh-mode", default="hres")
    ap.add_argument("--bh-mt", type=int, default=31)
    ap.add_argument("--color-temp", type=int, default=230)
    ap.add_argument("--no-ambient", action="store_true")
    ap.add_argument("--max-consecutive-fail", type=int, default=3)
    ap.add_argument("--no-onoff", action="store_true",
                    help="plain moveToLevel: needed to light level 1, which with-on-off switches OFF (ZCL min-level rule)")
    a = ap.parse_args()

    log = lambda *x: print(*x, file=sys.stderr, flush=True)
    levels = parse_levels(a.levels)
    fx = Fixture(a.device, log=log)
    sn = Sensors(bh_mode=a.bh_mode, bh_mt=a.bh_mt, tsl_gain=a.tsl_gain, tsl_atime=a.tsl_atime)
    log("#", sn.describe())
    log(f"# device={a.device} levels={len(levels)} reads={a.reads} settle={a.settle}s dirs={a.dirs}")

    f = open(a.out, "w", newline="")
    w = csv.writer(f)
    w.writerow(COLS)
    f.flush()
    fails = 0

    def row(phase, level, res):
        nonlocal fails
        t_pub, t_ack, attempts, st = res
        if t_ack is None:
            fails += 1
            confirm, ack_s = "none", ""
        else:
            fails = 0
            confirm, ack_s = "z2m", f"{t_ack - t_pub:.2f}"
        time.sleep(a.settle)
        bh, c0, c1 = sn.sample(a.reads)
        bm, bs, bmin, bmax = stats(bh)
        c0m, c0s, c0min, c0max = stats(c0)
        c1m, c1s, c1min, c1max = stats(c1)
        sat = ("BH" if bmax >= BH_SAT else "") + ("TSL" if sn.tsl_saturated(c0max) else "")
        zb = st.get("brightness") if st else ""
        w.writerow([f"{time.time():.3f}", phase, level, confirm, ack_s, attempts, a.reads,
                    f"{bm:.1f}", f"{bs:.2f}", bmin, bmax,
                    f"{c0m:.1f}", f"{c0s:.2f}", c0min, c0max,
                    f"{c1m:.1f}", f"{c1s:.2f}", c1min, c1max, sat, zb])
        f.flush()
        log(f"{phase:12s} L={level:>4} {confirm:4s} ack={ack_s or '-':>5}s  "
            f"BH {bm:8.1f} ±{bs:6.2f}   ch0 {c0m:8.1f} ±{c0s:6.2f}   ch1 {c1m:7.1f} ±{c1s:5.2f} {sat}")
        if fails >= a.max_consecutive_fail:
            log(f"!! {fails} consecutive unconfirmed commands - link is down, stopping")
            return False
        return True

    try:
        # colour temperature first, so every level renders the same CW/WW split
        r = fx.color_temp(a.color_temp)
        log(f"# color_temp {a.color_temp}: {'ack %.2fs' % (r[1]-r[0]) if r[1] else 'NO ACK'}")
        if not a.no_ambient:
            if not row("ambient_pre", 0, fx.off()):
                return 2
        if a.no_onoff:
            fx.brightness(max(levels), with_onoff=True)      # make sure it is ON before plain moveToLevel
        for d in a.dirs.split(","):
            seq = levels if d == "up" else list(reversed(levels))
            for L in seq:
                if not row(d, L, fx.brightness(L, with_onoff=not a.no_onoff)):
                    return 2
        if not a.no_ambient:
            row("ambient_post", 0, fx.off())
    finally:
        f.close()
        fx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
