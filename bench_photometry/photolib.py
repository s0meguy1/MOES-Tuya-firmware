#!/usr/bin/env python3
"""Bench photometry: BH1750 (0x23) + TSL2591 (0x29) on /dev/i2c-1, plus a
zigbee2mqtt driver for the bench fixture with confirm-and-retry.

Raw counts are the signal. Lux is never computed here; everything we care about
is a ratio. See AI_DROPOUT_BRIEF.md section 4 and 5.
"""
import json
import statistics
import threading
import time

from smbus2 import SMBus, i2c_msg

BH = 0x23
TSL = 0x29
TSL_CMD = 0xA0                       # command | normal transaction, auto-increment
TSL_GAIN = {"1x": 0x00, "25x": 0x10, "428x": 0x20, "9876x": 0x30}
TSL_ATIME = {100: 0x00, 200: 0x01, 300: 0x02, 400: 0x03, 500: 0x04, 600: 0x05}
# Full-scale count per integration time (datasheet: 100 ms is 36863, otherwise 65535)
TSL_SAT = {100: 36863, 200: 65535, 300: 65535, 400: 65535, 500: 65535, 600: 65535}
BH_MODES = {"hres": 0x10, "hres2": 0x11, "lres": 0x13}      # continuous modes
BH_BASE_MS = {"hres": 120, "hres2": 120, "lres": 16}          # typ. at MTreg 69
BH_SAT = 65535


class Sensors:
    """Both sensors, fixed configuration for the life of the object.
    One gain setting per pass - never switch mid-sweep (brief section 5).
    Sensitivity scales with MTreg/69 for the BH1750 and with the measured gain
    ratios 1.0/26.0/445.3 (not nominal 1/25/428) for the TSL2591."""

    def __init__(self, bus=1, bh_mode="hres", bh_mt=31, tsl_gain="1x", tsl_atime=100):
        # MTreg 31 is the default on purpose: at this jig distance the fixture at level
        # 254 reads ~49k counts at MTreg 31 and SATURATES (65535) at the part default 69.
        # MTreg persists in the sensor across power-on/reset opcodes, so always set it.
        self.b = SMBus(bus)
        self.lock = threading.Lock()
        self.bh_mode, self.bh_mt = bh_mode, int(bh_mt)
        self.tsl_gain, self.tsl_atime = tsl_gain, int(tsl_atime)
        self.bh_period = BH_BASE_MS[bh_mode] * self.bh_mt / 69.0 / 1000.0
        self.tsl_period = self.tsl_atime / 1000.0
        self._bh_init()
        self._tsl_init()
        time.sleep(max(self.bh_period, self.tsl_period) * 2.5)   # first full integrations

    def _bh_init(self):
        b = self.b
        b.write_byte(BH, 0x01); time.sleep(0.01)                  # power on
        b.write_byte(BH, 0x07); time.sleep(0.01)                  # reset data register
        mt = self.bh_mt
        b.write_byte(BH, 0x40 | (mt >> 5))                        # MTreg high 3 bits
        b.write_byte(BH, 0x60 | (mt & 0x1F))                      # MTreg low 5 bits
        b.write_byte(BH, BH_MODES[self.bh_mode])

    def _tsl_init(self):
        b = self.b
        self.tsl_id = b.read_byte_data(TSL, TSL_CMD | 0x12)       # expect 0x50
        b.write_byte_data(TSL, TSL_CMD | 0x00, 0x03)              # PON | AEN
        b.write_byte_data(TSL, TSL_CMD | 0x01, TSL_GAIN[self.tsl_gain] | TSL_ATIME[self.tsl_atime])

    def read_bh(self):
        with self.lock:
            r = i2c_msg.read(BH, 2)
            self.b.i2c_rdwr(r)
            d = list(r)
        return (d[0] << 8) | d[1]

    def read_tsl(self):
        # one transaction for all four bytes so ch0/ch1 come from the same integration
        with self.lock:
            d = self.b.read_i2c_block_data(TSL, TSL_CMD | 0x14, 4)
        return d[0] | (d[1] << 8), d[2] | (d[3] << 8)

    def tsl_saturated(self, c0):
        return c0 >= TSL_SAT[self.tsl_atime]

    def sample(self, n, spacing=None):
        """n reads of both sensors, spaced by the slower integration period so
        every read is a fresh integration, never a repeat of the last one."""
        sp = spacing or max(self.bh_period, self.tsl_period) * 1.1
        bh, c0, c1 = [], [], []
        for _ in range(n):
            t = time.time()
            bh.append(self.read_bh())
            a, b_ = self.read_tsl()
            c0.append(a); c1.append(b_)
            dt = sp - (time.time() - t)
            if dt > 0:
                time.sleep(dt)
        return bh, c0, c1

    def describe(self):
        return (f"BH1750 {self.bh_mode} MTreg={self.bh_mt} (~{self.bh_period*1000:.0f} ms)  "
                f"TSL2591 id=0x{self.tsl_id:02X} gain={self.tsl_gain} atime={self.tsl_atime} ms")


def stats(v):
    m = statistics.mean(v)
    s = statistics.pstdev(v)
    return m, s, min(v), max(v)


class Fixture:
    """One z2m device. Every set is confirmed by the state publish z2m emits
    after the device's ZCL default response, and retried on silence. z2m does
    not publish its errors to MQTT, so silence is all a timeout looks like."""

    def __init__(self, name="moes_bench", host="192.168.1.30", port=1883, base="zigbee2mqtt", log=print):
        import paho.mqtt.client as mqtt
        self.name = name
        self.topic = f"{base}/{name}"
        self.log = log
        self.msgs = []                 # (t_received, dict)
        self.lock = threading.Lock()
        if hasattr(mqtt, "CallbackAPIVersion"):
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        else:
            self.c = mqtt.Client()
        self.c.on_message = self._on_msg
        self.c.connect(host, port, 10)
        self.c.subscribe(self.topic)
        self.c.loop_start()
        time.sleep(0.5)

    def _on_msg(self, c, u, m):
        try:
            d = json.loads(m.payload)
        except Exception:
            return
        with self.lock:
            self.msgs.append((time.time(), d))

    def state_after(self, t, pred):
        with self.lock:
            for ts, d in self.msgs:
                if ts >= t and pred(d):
                    return ts, d
        return None

    def last_state(self):
        with self.lock:
            return self.msgs[-1] if self.msgs else None

    def set(self, payload, pred, timeout=12.0, retries=3):
        """Returns (t_pub, t_ack or None, attempts, state or None)."""
        t = time.time()
        for attempt in range(1, retries + 1):
            t = time.time()
            self.c.publish(f"{self.topic}/set", json.dumps(payload))
            deadline = t + timeout
            while time.time() < deadline:
                r = self.state_after(t, pred)
                if r:
                    return t, r[0], attempt, r[1]
                time.sleep(0.02)
            self.log(f"  no ack for {payload} within {timeout:.0f}s (attempt {attempt}/{retries})")
        return t, None, retries, None

    def level_cmd(self, lvl, transtime_ds=0, with_onoff=True):
        """Raw ZCL moveToLevelWithOnOff. This bypasses the external converter's
        BRIGHTNESS_FLOOR=13 wrapper (moes_ts0505b_light_show.js), which silently
        rewrites any z2m `brightness` below 13 to 13 - the whole region the
        dropout lives in. Returns the publish time; nothing is published back."""
        cmd = {"command": {"cluster": "genLevelCtrl",
                           "command": "moveToLevelWithOnOff" if with_onoff else "moveToLevel",
                           "payload": {"level": int(lvl), "transtime": int(transtime_ds)}}}
        t = time.time()
        self.c.publish(f"{self.topic}/set", json.dumps(cmd))
        return t

    def read_brightness(self, timeout=10.0, with_state=False):
        """z2m `get` of currentLevel (and onOff when with_state). A raw command
        does not update z2m's cached state, so `state` in a brightness-only
        publish is stale; ask for both when the on/off state matters.
        Returns (t_publish, brightness, state) or (None, None, None)."""
        tg = time.time()
        req = {"brightness": ""}
        if with_state:
            req["state"] = ""
        self.c.publish(f"{self.topic}/get", json.dumps(req))
        deadline = tg + timeout
        seen = 0
        while time.time() < deadline:
            with self.lock:
                fresh = [(ts, d) for ts, d in self.msgs if ts >= tg]
            if len(fresh) >= (2 if with_state else 1):
                ts, d = fresh[-1]
                return ts, d.get("brightness"), d.get("state")
            time.sleep(0.02)
        return None, None, None

    def brightness(self, lvl, transition=0, timeout=12.0, retries=3, with_onoff=True):
        """Set a level with no ramp and confirm it by reading currentLevel back.
        Returns (t_pub, t_ack or None, attempts, state or None)."""
        t = time.time()
        for attempt in range(1, retries + 1):
            t = self.level_cmd(lvl, int(round(transition * 10)), with_onoff=with_onoff)
            time.sleep(0.25)
            ta, bri, st = self.read_brightness(timeout=timeout, with_state=True)
            if ta is not None and bri == lvl and st == "ON":
                return t, ta, attempt, {"brightness": bri, "state": st}
            self.log(f"  level {lvl}: readback {bri!r}/{st!r} (attempt {attempt}/{retries})")
        return t, None, retries, None

    def off(self, transition=0, **kw):
        return self.set({"state": "OFF", "transition": transition},
                        lambda d: d.get("state") == "OFF", **kw)

    def color_temp(self, mireds, **kw):
        return self.set({"color_temp": mireds, "transition": 0},
                        lambda d: d.get("color_temp") == mireds, **kw)

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


def parse_levels(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out
