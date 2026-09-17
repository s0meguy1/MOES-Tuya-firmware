#!/usr/bin/env python3
"""Test D of AI_DROPOUT_BRIEF.md: log both sensors continuously through a
fade, with the command's publish time and z2m ack time as reference marks.

  fade.py --out fade8.csv --seconds 8              # 254 -> off over 8 s
  fade.py --out fade20.csv --seconds 20 --tsl-gain 25x
  fade.py --out up.csv --seconds 8 --from off --to 254

BH1750 default here is H-res at MTreg 31 (~54 ms integration, sensitivity x0.45)
for time resolution; TSL2591 100 ms. Every read is stored with its timestamp;
repeated values from the same integration are left for the analysis to fold.
"""
import argparse
import csv
import sys
import threading
import time

from photolib import Fixture, Sensors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="moes_bench")
    ap.add_argument("--seconds", type=float, required=True, help="transition length sent to z2m")
    ap.add_argument("--from", dest="frm", default="254", help="starting level, or 'off'")
    ap.add_argument("--to", default="off", help="target level, or 'off'")
    ap.add_argument("--pre", type=float, default=2.0, help="seconds logged before the command")
    ap.add_argument("--post", type=float, default=3.0, help="seconds logged after the fade should end")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds at the start level before logging")
    ap.add_argument("--tsl-gain", default="1x")
    ap.add_argument("--tsl-atime", type=int, default=100)
    ap.add_argument("--bh-mode", default="hres")
    ap.add_argument("--bh-mt", type=int, default=31)
    ap.add_argument("--color-temp", type=int, default=230)
    ap.add_argument("--poll", type=float, default=0.012)
    a = ap.parse_args()
    log = lambda *x: print(*x, file=sys.stderr, flush=True)

    fx = Fixture(a.device, log=log)
    sn = Sensors(bh_mode=a.bh_mode, bh_mt=a.bh_mt, tsl_gain=a.tsl_gain, tsl_atime=a.tsl_atime)
    log("#", sn.describe())

    r = fx.color_temp(a.color_temp)
    log(f"# color_temp {a.color_temp}: {'ack %.2fs' % (r[1]-r[0]) if r[1] else 'NO ACK'}")
    if a.frm == "off":
        r = fx.off()
    else:
        r = fx.brightness(int(a.frm))
    if r[1] is None:
        log("!! could not establish the start level, aborting")
        return 2
    log(f"# start level {a.frm} acked in {r[1]-r[0]:.2f}s; settling {a.settle}s")
    time.sleep(a.settle)

    rows = []
    stop = threading.Event()

    def sampler():
        last_bh = None
        last_tsl = None
        while not stop.is_set():
            t = time.time()
            bh = sn.read_bh()
            c0, c1 = sn.read_tsl()
            rows.append((t, bh, c0, c1))
            dt = a.poll - (time.time() - t)
            if dt > 0:
                time.sleep(dt)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    time.sleep(a.pre)

    tgt = 0 if a.to == "off" else int(a.to)
    transtime = int(round(a.seconds * 10))
    t_pub = fx.level_cmd(tgt, transtime)          # raw ZCL, same frame z2m sends for state OFF + transition
    time.sleep(0.3)
    t_ack, bri, st = fx.read_brightness(timeout=10.0)
    payload = f"moveToLevelWithOnOff(level={tgt}, transtime={transtime})"
    log(f"# published {payload} at {t_pub:.3f}; readback " + (f"+{t_ack - t_pub:.3f}s brightness={bri} state={st}" if t_ack else "NONE"))
    time.sleep(a.seconds + a.post)
    stop.set()
    th.join(timeout=2)
    fx.close()

    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["# device", a.device, "seconds", a.seconds, "from", a.frm, "to", a.to,
                    "t_pub", f"{t_pub:.3f}", "t_readback", f"{t_ack:.3f}" if t_ack else "", "readback_level", bri,
                    "sensors", sn.describe()])
        w.writerow(["t_rel_pub", "bh", "c0", "c1"])
        for t, bh, c0, c1 in rows:
            w.writerow([f"{t - t_pub:.3f}", bh, c0, c1])

    # coarse summary: BH at half-second marks, relative to the pre-fade mean
    pre = [bh for t, bh, _, _ in rows if t < t_pub - 0.2]
    base = sum(pre) / len(pre) if pre else float("nan")
    log(f"# {len(rows)} samples; pre-fade BH mean {base:.1f}")
    marks = [x * 0.5 for x in range(int(a.seconds / 0.5) + 3)]
    for m in marks:
        near = [bh for t, bh, _, _ in rows if abs((t - t_pub) - m) < 0.03]
        if near:
            v = sum(near) / len(near)
            log(f"  t={m:5.1f}s  BH {v:8.1f}  {100*v/base:6.2f}%  cbrt {100*(max(v,0)/base)**(1/3):6.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
