#!/usr/bin/env python3
"""Phase C: isolate the cue-count discrepancy. Uploads lists of 1, 5, 8, 22 entries
(count read back after each), then saves a real 5-entry list to slot 1 and recalls
slots 0 and 1. No show is run."""
import json, sys, time
from photolib import Fixture
sys.path.insert(0, ".")
from show_test import LIST_A

LIST_B = [
  {"t": 0, "effect": "solid", "hue": 120, "saturation": 100, "level": 254},
  {"t": 1000, "level": 60}, {"t": 2000, "level": 254}, {"t": 3000, "level": 60},
  {"t": 4000, "effect": "stop"},
]
def main():
    log = lambda *x: print(time.strftime("%H:%M:%S"), *x, flush=True)
    fx = Fixture("moes_bench", log=log)
    def show_get(timeout=8):
        t0 = time.time(); n0 = len(fx.msgs)
        fx.c.publish(f"{fx.topic}/get", json.dumps({"light_show": ""}))
        while time.time() - t0 < timeout:
            with fx.lock:
                new = [d for ts, d in fx.msgs[n0:] if "light_show_cue_count" in d]
            if new: return new[-1]
            time.sleep(0.05)
        return None
    def st(d): return {k: d.get(k) for k in ("light_show_cue_count", "light_show_cue_run", "light_show_cue_slots")} if d else None
    def setp(payload, wait=1.5):
        fx.c.publish(f"{fx.topic}/set", json.dumps(payload)); time.sleep(wait)
    log("start:", st(show_get()))
    for n in (1, 5, 8, 22):
        setp({"light_show_cue_list": LIST_A[:n]}, wait=1.0 + 0.4 * ((n + 6) // 7))
        log(f"upload {n} entries -> count", st(show_get())["light_show_cue_count"])
    setp({"light_show_cue_list": LIST_B}); log("upload B (5, no level 255) -> count", st(show_get())["light_show_cue_count"])
    setp({"light_show_cue_save": 1}); log("save slot 1:", st(show_get()))
    setp({"light_show_cue_recall": 0}); log("recall 0 (A):", st(show_get()))
    setp({"light_show_cue_recall": 1}); log("recall 1 (B, expect 5):", st(show_get()))
    setp({"light_show_cue_recall": 2}); log("recall 2 (empty slot, expect unchanged/rejected):", st(show_get()))
    fx.close()
if __name__ == "__main__": main()
