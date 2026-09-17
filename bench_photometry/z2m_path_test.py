#!/usr/bin/env python3
"""End-to-end check of the NORMAL zigbee2mqtt path after the converter floor was
removed: `brightness` values below 13 must arrive as sent, and a `state: OFF`
with a transition must fade to black with no plateau. Uses z2m's own set topic
(no raw ZCL), reads back via /get, and logs the photometer through the fade."""
import json, statistics, sys, threading, time
from photolib import Fixture, Sensors

def main():
    log = lambda *x: print(time.strftime("%H:%M:%S"), *x, flush=True)
    fx = Fixture("moes_bench", log=log); sn = Sensors(bh_mt=31)
    def z2m_set(payload, wait=1.5):
        fx.c.publish(f"{fx.topic}/set", json.dumps(payload)); time.sleep(wait)
    def readback():
        ta, bri, st = fx.read_brightness(timeout=8, with_state=True); return bri, st
    def light():
        bh, c0, c1 = sn.sample(5); return statistics.mean(bh)
    z2m_set({"state": "ON", "brightness": 254}, 2.0); full = light(); log(f"254 via z2m: readback {readback()} BH {full:.0f}")
    for b in (1, 2, 5, 12):
        z2m_set({"brightness": b}); rb = readback(); v = light()
        log(f"brightness {b} via z2m: readback {rb}  light {100*v/full:5.2f}% of full  (floor-13 would have given ~20%)")
    z2m_set({"brightness": 254}, 2.0)
    rows = []; stop = threading.Event()
    def sampler():
        while not stop.is_set(): rows.append((time.time(), sn.read_bh())); time.sleep(0.05)
    th = threading.Thread(target=sampler, daemon=True); th.start(); time.sleep(1.5)
    t0 = time.time(); fx.c.publish(f"{fx.topic}/set", json.dumps({"state": "OFF", "transition": 6}))
    time.sleep(6 + 2.5); stop.set(); th.join(1)
    base = statistics.mean([v for t, v in rows if t < t0])
    log("fade to off via z2m (6 s): light % of full every 0.5 s")
    for m in [x * 0.5 for x in range(0, 15)]:
        near = [v for t, v in rows if abs(t - t0 - m) < 0.06]
        if near: print(f"   t={m:4.1f}s {100*statistics.mean(near)/base:6.2f}%")
    lit = [(t, v) for t, v in rows if t > t0 and v > base * 0.005]
    if lit: log(f"last lit sample t={lit[-1][0]-t0:.2f}s at {100*lit[-1][1]/base:.2f}% of full")
    log("final readback:", readback())
    fx.close()
if __name__ == "__main__": main()
