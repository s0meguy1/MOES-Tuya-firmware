#!/usr/bin/env python3
"""Sequential, verified OTA rollout for the Moes TS0505B fleet.

Deliberately boring and slow: ONE light at a time, each one verified before
the next is touched, and nothing is ever broadcast. It talks to zigbee2mqtt
over MQTT using the per-device `url` form of ota_update/update, so no OTA
index exists that another device could accidentally match.

Safety properties relied on:
  * a stalled/failed download is a no-op - the light keeps running whatever
    it was running
  * the light's IEEE is preserved by the firmware, so z2m keeps the same
    device entry, friendly name and Home Assistant entities

Usage:
  # see what would happen, touch nothing
  ./rollout.py --list
  ./rollout.py --dry-run

  # convert one specific light and stop
  ./rollout.py --only 0xa4c138…c03e

  # roll the fleet, pausing for confirmation after each success
  ./rollout.py --confirm-each

  # unattended, but stop the moment anything fails
  ./rollout.py --max-failures 1
"""

import argparse
import json
import subprocess
import sys
import time

MQTT_HOST = "localhost"
BASE = "zigbee2mqtt"
DOCKER_MQTT = ["docker", "exec", "mosquitto"]

# lights still on stock firmware take the conversion image; lights already
# converted take the normal one
CONVERSION_URL = "http://<bench-pi>:8093/1141-d3a3-ffffffff-moes-conv-v1.zigbee"
UPDATE_URL = "http://<bench-pi>:8093/6464-0395-11023003-light_TS0505B.zigbee"

STOCK_VERSION = 101          # what stock firmware reports
MODEL = "ZB-TDD6-RCW-4"

OTA_TIMEOUT = 45 * 60        # generous; transfers observed at 15-40 min
POLL = 15


def sh(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout


def mqtt_pub(topic, payload):
    sh(DOCKER_MQTT + ["mosquitto_pub", "-h", MQTT_HOST, "-t", topic, "-m", payload])


def mqtt_get(topic, timeout=15):
    out = sh(DOCKER_MQTT + ["mosquitto_sub", "-h", MQTT_HOST, "-t", topic, "-C", "1", "-W", str(timeout)],
             timeout=timeout + 10)
    try:
        return json.loads(out.strip())
    except Exception:
        return None


def fleet():
    """Every light z2m knows about that matches our model."""
    devices = mqtt_get(f"{BASE}/bridge/devices", timeout=20) or []
    out = []
    for d in devices:
        defn = d.get("definition") or {}
        if defn.get("model") == MODEL and d.get("type") != "Coordinator":
            out.append({
                "ieee": d["ieee_address"],
                "name": d.get("friendly_name", d["ieee_address"]),
                "ota": defn.get("supports_ota"),
            })
    return sorted(out, key=lambda x: x["name"])


def state_of(ieee):
    return mqtt_get(f"{BASE}/{ieee}", timeout=10) or {}


def version_of(ieee):
    st = state_of(ieee)
    return (st.get("update") or {}).get("installed_version")


def wait_online(ieee, timeout=180):
    """Ask the device for its state until it answers."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        mqtt_pub(f"{BASE}/{ieee}/get", '{"state":""}')
        time.sleep(POLL)
        st = state_of(ieee)
        if st.get("linkquality") is not None:
            return True
    return False


def update_one(dev, url, dry_run=False):
    ieee, name = dev["ieee"], dev["name"]
    before = version_of(ieee)
    print(f"\n=== {name} ({ieee})")
    print(f"    installed_version before: {before}")

    if dry_run:
        print(f"    DRY RUN - would POST ota_update/update with {url}")
        return True

    if not dev.get("ota"):
        print("    SKIP: z2m reports supports_ota=false (external converter not loaded?)")
        return False

    mqtt_pub(f"{BASE}/bridge/request/device/ota_update/update",
             json.dumps({"id": ieee, "url": url}))
    print(f"    started {time.strftime('%H:%M:%S')} - polling (a stall here is harmless)")

    deadline = time.time() + OTA_TIMEOUT
    last = None
    while time.time() < deadline:
        time.sleep(POLL)
        upd = (state_of(ieee).get("update") or {})
        st, prog = upd.get("state"), upd.get("progress")
        if prog is not None and prog != last:
            print(f"    {prog:5.1f}%  ({upd.get('remaining', '?')}s left)")
            last = prog
        if st == "idle" and prog in (None, 100):
            break
        if st == "available":
            break

    print("    transfer window closed; waiting for the device to come back")
    if not wait_online(ieee, timeout=300):
        print("    *** device did not respond after the update window ***")
        return False

    after = version_of(ieee)
    print(f"    installed_version after: {after}")
    if after is not None and before is not None and after != before:
        print("    OK - version changed, device online")
        return True
    print("    no version change (download probably stalled) - device is unharmed")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="show the fleet and exit")
    ap.add_argument("--dry-run", action="store_true", help="plan only, send nothing")
    ap.add_argument("--only", help="single IEEE to act on")
    ap.add_argument("--confirm-each", action="store_true", help="pause after every device")
    ap.add_argument("--max-failures", type=int, default=1, help="abort after N failures (default 1)")
    ap.add_argument("--url", help="override the image URL")
    args = ap.parse_args()

    lights = fleet()
    if not lights:
        print("no lights found - is z2m reachable and the converter loaded?")
        return 1

    if args.only:
        lights = [d for d in lights if d["ieee"].lower() == args.only.lower()]
        if not lights:
            print(f"{args.only} not found")
            return 1

    print(f"{len(lights)} light(s) in scope; supports_ota true on "
          f"{sum(1 for d in lights if d['ota'])}")
    if args.list:
        for d in lights:
            print(f"  {d['ieee']}  ota={d['ota']!s:5}  {d['name']}")
        return 0

    failures = 0
    for i, dev in enumerate(lights, 1):
        cur = version_of(dev["ieee"])
        url = args.url or (CONVERSION_URL if cur == STOCK_VERSION else UPDATE_URL)
        print(f"\n[{i}/{len(lights)}] {dev['name']}  (version {cur} -> "
              f"{'conversion' if url == CONVERSION_URL else 'update'} image)")

        if not update_one(dev, url, dry_run=args.dry_run):
            failures += 1
            if failures >= args.max_failures:
                print(f"\nstopping: {failures} failure(s), limit {args.max_failures}")
                return 1

        if args.confirm_each and i < len(lights):
            if input("\ncontinue to the next light? [y/N] ").strip().lower() != "y":
                print("stopped by operator")
                return 0

    print(f"\ndone: {len(lights) - failures} succeeded, {failures} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
