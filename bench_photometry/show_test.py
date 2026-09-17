#!/usr/bin/env python3
"""Build 43 stored-show test on the bench fixture, over z2m.

Phase A (no reboot needed): upload list A (22 entries), save slot 0; upload list B
(5 entries), save slot 1; recall 0 -> count 22; recall 1 -> count 5; then
{"cue_recall":0,"cue_run":"run"} and log the photometer for the show.
Phase B (--after-reboot): after a mains cycle, /get must show count 22 and slots
"0,1" with no upload; run it again.

  show_test.py --phase a    |    show_test.py --phase b
"""
import argparse, json, sys, time, threading
from photolib import Fixture, Sensors

LIST_A = [
  {"t": 0,     "effect": "burst",   "hue": 220, "saturation": 100, "level": 254},
  {"t": 1450,  "effect": "twinkle"},
  {"t": 12500, "hue": 0},
  {"t": 13250, "speed": 1, "effect": "strobe"},
  {"t": 15700, "speed": 3},  {"t": 17300, "speed": 6},  {"t": 18900, "speed": 12},
  {"t": 20000, "speed": 20}, {"t": 21000, "speed": 32}, {"t": 21800, "speed": 55},
  {"t": 22500, "speed": 100},
  {"t": 23190, "effect": "explode", "speed": 100},
  {"t": 23950, "effect": "fire"},
  {"t": 25190, "speed": 55}, {"t": 27190, "speed": 35},
  {"t": 28500, "speed": 100},
  {"t": 29800, "effect": "explode"},
  {"t": 30600, "effect": "fire"},
  {"t": 31600, "speed": 40}, {"t": 32500, "speed": 12},
  {"t": 33600, "effect": "stop", "level": 0, "fade": 1000},
]
LIST_B = [
  {"t": 0, "effect": "solid", "hue": 120, "saturation": 100, "level": 254},
  {"t": 1000, "level": 60},
  {"t": 2000, "level": 254},
  {"t": 3000, "level": 60},
  {"t": 4000, "effect": "stop", "level": 255},
]

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--phase", default="a"); ap.add_argument("--device", default="moes_bench")
    a = ap.parse_args()
    log = lambda *x: print(time.strftime("%H:%M:%S"), *x, flush=True)
    fx = Fixture(a.device, log=log)

    def show_get(timeout=8):
        t0 = time.time(); n0 = len(fx.msgs)
        fx.c.publish(f"{fx.topic}/get", json.dumps({"light_show": ""}))
        while time.time() - t0 < timeout:
            with fx.lock:
                new = [d for ts, d in fx.msgs[n0:] if "light_show_cue_count" in d]
            if new: return new[-1]
            time.sleep(0.05)
        return None
    def st(d): return {k: d.get(k) for k in ("light_show", "light_show_cue_count", "light_show_cue_run", "light_show_cue_slots")} if d else None
    def setp(payload, wait=1.2):
        fx.c.publish(f"{fx.topic}/set", json.dumps(payload)); time.sleep(wait)

    log("start:", st(show_get()))
    if a.phase == "a":
        setp({"light_show_cue_list": LIST_A}, wait=3.0); log("after upload A (22):", st(show_get()))
        setp({"light_show_cue_save": 0});            log("after save slot 0:", st(show_get()))
        setp({"light_show_cue_list": LIST_B}, wait=2.0); log("after upload B (5):", st(show_get()))
        setp({"light_show_cue_save": 1});            log("after save slot 1:", st(show_get()))
        setp({"light_show_cue_recall": 0});          log("after recall 0 (expect 22):", st(show_get()))
        setp({"light_show_cue_recall": 1});          log("after recall 1 (expect 5):", st(show_get()))
        setp({"light_show_cue_save": 3});            log("save slot 3 too (expect slots 0,1,3):", st(show_get()))
    else:
        log("after reboot, no upload: expect count 22, slots 0,1,3")
    # run the stored show from slot 0 and log the photometer
    sn = Sensors(bh_mt=31)
    rows = []; stop = threading.Event()
    def sampler():
        while not stop.is_set():
            rows.append((time.time(), sn.read_bh())); time.sleep(0.05)
    th = threading.Thread(target=sampler, daemon=True); th.start()
    t0 = time.time()
    setp({"light_show_cue": {"cue_recall": 0, "cue_run": "run"}}, wait=0.5)
    log("recall 0 + run in one frame:", st(show_get()))
    dur = 36 if a.phase == "a" else 36
    time.sleep(dur); stop.set(); th.join(1)
    log("after the show:", st(show_get()))
    # coarse timeline: 1 s bins of BH mean/min/max
    import statistics
    print("  t(s)   mean    min    max   (BH counts, MTreg 31)")
    for b in range(0, dur, 2):
        v = [x for t, x in rows if b <= t - t0 < b + 2]
        if v: print(f"  {b:4d}  {statistics.mean(v):6.0f} {min(v):6d} {max(v):6d}")
    fx.close()

if __name__ == "__main__":
    sys.exit(main())
