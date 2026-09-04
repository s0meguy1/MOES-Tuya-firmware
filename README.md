# MOES / ZT3L firmware research

> ## → The firmware lives here: **[s0meguy1/tuyaZigbee](https://github.com/s0meguy1/tuyaZigbee)**
>
> That is the project: source, documentation, and
> **[prebuilt releases](https://github.com/s0meguy1/tuyaZigbee/releases)** with
> published checksums. Start there.
>
> This repository is only the bench workspace behind it — hardware tooling and
> research notes. It is kept alive because links to it are already circulating;
> everything about the firmware itself is maintained in the fork, not here.

An experimental workspace for Moes RGB+CCT downlights (`TS0505B`) built on the
Tuya ZT3L module (Telink TLSR8258).

## What it looks like

Both clips are one bench fixture running the on-device light-show engine. The
effect is rendered on the chip; the network is only told *which* effect to run,
not fed a frame at a time. One group broadcast runs a whole room.

| flash | multi-scene |
| :---: | :---: |
| ![A downlight strobing red on a bench](media/light-show-flash.gif) | ![A downlight stepping through red, cyan and green](media/light-show-multi-scene.gif) |

Fifteen effects, a 32-step cue list the chip plays by itself, and per-fixture
phase offsets so one broadcast becomes a chase across a room. Full reference:
[`docs/light_show.md`](https://github.com/s0meguy1/tuyaZigbee/blob/main/docs/light_show.md).

## The hardware

No affiliation, no referral codes — these are here so you can check you have the
same board before flashing anything.

- **The light** — Moes ZB-TDD6-RCW-4 RGB+CCT downlight:
  <https://www.aliexpress.us/item/3256806801178235.html>
- **The bare module** — Tuya ZT3L (Telink TLSR8258):
  <https://www.aliexpress.us/item/3256809199488861.html>

Confirm before you flash: Zigbee2MQTT should report `TS0505B` /
`_TZ3210_b8jdosxo`, the IEEE address should begin `0xa4c138` (a Telink chip),
and the module inside should be marked `ZT3L`.

## Installing it

**All of it is documented in the fork**, and that is the copy kept current:

- [Download a release](https://github.com/s0meguy1/tuyaZigbee/releases) and
  verify its sha256
- [Conversion procedure](https://github.com/s0meguy1/tuyaZigbee/blob/main/docs/moes_ts0505b_conversion.md)
  — read this before converting a stock device
- [What the firmware does, and what is proven](https://github.com/s0meguy1/tuyaZigbee#moes-ts0505b-custom-firmware)

One warning is worth repeating rather than linking, because it costs everyone
the same day: **conversion erases NV, so the fixture must rejoin, and you have
to open permit-join _after_ the install finishes.** Until you do, a healthy
fixture is indistinguishable from a dead one. Zigbee2MQTT caps permit-join at
254 seconds while a conversion takes 35–85 minutes, so a window opened when you
start is always gone by the time the device needs it.

## What is actually in this repository

Bench-side work that has no place in the firmware tree:

- `dump/TLSR825xComFlasher.working.py`, `dump/merge_verify.py`,
  `dump/swire_diag.py` — SWire research tools. Treat hardware interaction as
  read-only unless a reviewed procedure says otherwise.
- Notes on restoring stock onto a converted fixture: splice the stock app and
  config regions from a donor dump, **erase** the NV region rather than writing
  the donor's, and never touch the bootloader or the identity/calibration
  region. A donor dump carries the donor's MAC inside its NV region — writing
  that region onto another board puts a duplicate MAC on your mesh.

## Privacy and repository history

Bench captures, firmware readouts, logs, session notes and per-device forensics
are deliberately kept out of both repositories, and the firmware checkout has an
automated guard that fails if a full device address or a designated private
forensic note reaches the published branch.

A removal from the current index does **not** erase material already in
published history. Do not assume a later cleanup commit purges prior captures;
history rewriting or credential rotation requires explicit owner approval.
