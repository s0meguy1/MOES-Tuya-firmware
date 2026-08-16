# MOES Firmware Extraction — Handoff

**Status 2026-08-14:** SWire flash reads WORK. ~25 KB of the 1 MB image comes
off reliably and is verified-genuine Telink firmware. Reads stall partway and
do not recover — that one open problem is specced in **`DEBUG_BRIEF.md`**.

Three docs, no overlap:
- **`DEBUG_BRIEF.md`** — the actionable open problem. Self-contained; hand it to
  anyone picking this up.
- **`PI_SWIRE_SETUP.md`** — the Raspberry Pi programmer rig: UART config,
  wiring, the watchdog fix, run procedure.
- **this file** — project history and why things are the way they are.

---

## 0. Status 2026-08-16 ~23:00 UTC — build 09 closeout (read first)

**Where everything stands.** Build 09 exists and is Gate-A green, uncommitted
(branch `moes-ts0505b`, HEAD `87a4e2d`): 27/27 host tests; `APP_BUILD 0x09`;
image `6464-0395-11093003-light_TS0505B.zigbee` (202450 B, sha256
`54bdd8919b2d39e226d8c874217fc5bce2594cef85289b0884faa845c731149f`) at
`http://<ota-server>:8094/`. It fixes two things: (1) `ota_mcuReboot()` now
writes the 12-byte install descriptor `{0x70001, size, 1}` at `0xF7000` plus the
`0x4B` flag, then resets unconditionally — the missing descriptor is why
ours→ours OTA has never installed; (2) the liveness/rescue redesign (two-layer
heartbeat: `ev_timer` progress ticker + `drv_hwTmr` `TIMER_IDX_0` IRQ sampler,
joined-blind 60 s fuse; stable-clear requires joined AND progress + a 1-min
confirmation window; rescue mode queries OTA immediately on join).

**The bench unit is stranded on build 06 remotely.** It cannot install anything
OTA anymore (no descriptor writer on the device; SWire writes broken; UART
protocol unanswered). A stock unit always works because the stock app writes the
descriptor. Clean fleet path: **stock → build 09 conversion OTA → ours→ours
forever after.**

**When the user returns — physical options for the bench unit:**
1. Power-cycle is **pointless for OTA** (no descriptor writer on build 06), but a
   healthy boot still helps wedge SCIENCE.
2. Retry the UART bootloader protocol on module pins 15/16 with
   `uart_flash/uart_flash.py`.
3. Re-seat the dead GPIO17 RST wire and the known-flaky SWS pin-17 joint
   (`HANG_FINDINGS.md` §9).
4. Swap the ZT3L module.

**Fleet go/no-go (playbook §8, adapted).** The bench can no longer prove
installs. The **first stock-unit conversion IS the install proof**: pick ONE
stock ceiling fixture, run the announce watcher, and keep the previous image URL
as the rollback note — when the user approves.

**Build 09 review checklist for the user:** `git diff` of the tracked edits
(`common/version.h`, `MOES_EDITING_GUIDE.md` +2 SDK-patch rows,
`light/moes_liveness.{c,h}`, `light/moes_rescue.{c,h}`, `light/zb_appCb.c`,
`tools/rescue_hosttest/**`), the `ota.c` hunks (vendored/gitignored, described in
the guide rows), and the bughunt docs under `work/tuyaZigbee/bughunt/`
(`install_sm.md` is the authoritative root-cause).

**Two operational warnings:**
- `tools/tl_check_fw.py` is a **PATCHER, not a checker** — on a `.zigbee` it
  mangles the OTA header in place (it is for raw Telink `.bin` only). A Gate-A
  run had to rebuild the image after it rewrote one.
- `genOta.fileOffset 131250` / `imageUpgradeStatus 1` sits in the bench unit's NV
  from the build-07 transfer era — stale OTA stall state, not a live transfer.

---

## 1. Goal

Dump the 1 MB flash of the **Tuya ZT3L module (Telink TLSR8258)** on a Moes
ZB-TDD6-RCW-4 downlight (Zigbee `TS0505B` / `_TZ3210_b8jdosxo`, Tuya productId
`b8jdosxo`). 46 units deployed via Home Assistant + zigbee2mqtt.

**This is analysis value, not production-blocking.** The original driver was a
phantom-leave bug (koenkk/zigbee-herdsman#1648: spurious leave on power restore;
the device keeps the network key, only herdsman's `removeFromDatabase` breaks
it). That **already has a working software fix** — the z2m external extension
`ignore_device_leave` in Koenkk/zigbee2mqtt-user-extensions (PR #27, confirmed
in production with 37 devices). The dump is for root-cause analysis and possible
Koenkk/zigbee-OTA submission.

## 2. Cloud route — CLOSED, do not reopen

Fully exhausted 2026-08-11. **Tuya has no published OTA package for
`b8jdosxo`.** A working offline API rig was built (frida signing oracle in the
MOES Android app, host-side `api.json` calls with real native request signing)
and every firmware endpoint was swept: `m.thing.ota.firmware.info.get`,
`m.thing.firmware.upgrade.info.get`, `m.thing.device.product.check.for.updates`,
`m.life.device.upgrade.list` with a real home gid — all empty, no URL.

A sibling sweep of **all 37 `_TZ3210_*` productIds** sharing the z2m TS0505B_1
fingerprint returned empty upgradeLists, `currentVersion=1.0.0`,
`upgradeStatus=0` — no OTA has ever been published for the entire TS0505B ZS3L
family. Koenkk/zigbee-OTA `index.json` (894 entries): zero for TS0505B/b8jdosxo.
GitHub code search: `b8jdosxo` appears only in fingerprint lists. All 46 real
lights run `appVersion=101`, almost certainly the only build ever shipped.

The APK, decompiled trees, and frida/oracle scripts were **deleted 2026-08-14** —
they only served this dead route. Recoverable if ever needed: MOES 2.0.6 from
APKPure, then jadx. Extracted credentials, for the record: appId
`fyft4mrvd9jygmsqyumc`, appSecret `ejhnk5sgua8ypttestw4uptxt9rq3r5w`, US shard
`a1.tuyaus.com`. Sign chain ends in native `libthing_security`
(`doCommandNative`) — never reversed, an in-app oracle made it unnecessary.

**Physical extraction is the only remaining route to the binary.**

## 3. The module and its pads

**Tuya ZT3L = Telink TLSR825x** (module labeled ZT3L, QR 647548 / 100222218) —
NOT ZS3L/EFR32 as earlier guessed. No readout protection.

Datasheet pinout: 1=RESETB, 8=TL_VDD_3V3, 9=GND, 15=TL_B7 (RXD), 16=TL_B1 (TXD),
17=SWS. Module is 16 × 24 mm; pin 17 is a lone 1.2 mm pad on the short edge
opposite the antenna, 5.00 mm from the pin-1..8 edge.

**Orientation with the antenna at LEFT** (as in the board photos):

- **Bottom row = pins 1→8.** Pin 1 (RST) at the antenna end, pin 8 (3V3) at the
  electrolytic-cap end. Confirmed empirically — the chip powers from that pad.
- **Top row = pins 16→9.** Pin 16 (TXD) at the antenna end, pin 9 (GND) at the
  cap end.
- **Pin 17 (SWS)** = the lone pad on the cap-facing short edge, ~5 mm up from
  the bottom row.

**Datasheet drawings: `zt3l_docs/`** — the authoritative source, and what the
map above was derived from and then confirmed empirically.

**The board photos were lost in the 2026-08-14 cleanup** (`PXL_20260811_140713151.jpg`
driver board, `PXL_20260811_140727038.jpg` LED board, plus the annotated
`zt3l_pinout_annotated.jpg` / `zt3l_rst_fix.jpg`). Not in trash, unrecoverable
locally. The `PXL_` names mean they came off a Pixel, so they are very likely
still in Google Photos dated 2026-08-11 — pull them back from there rather than
tearing a light down again. The written pad map above does not depend on them.

Unambiguous phrasing that finally worked for locating RST:
***"same row as the 3.3V wire, opposite end."***

**Mains stays DISCONNECTED at all times** — the driver's low-voltage rail is not
isolated. Rig uses light unit #2; unit #1 possibly damaged, set aside.

## 4. Rig — Raspberry Pi 5. See `PI_SWIRE_SETUP.md`.

`ssh <bench-pi>`, toolkit in `~/tlsr`. The Pi's **PL011 is an on-chip
UART**, so there is no USB packet scheduling in the signal path and reset is a
real GPIO. That is the property that matters — the same one that makes a
TB-03F-KIT work. It is not the brand.

**The CP2102 rig never achieved sync and was abandoned.** ~320 activation
attempts across every parameter. A TB-03F-KIT was ordered as a fallback and is
weeks out; the Pi made it unnecessary.

## 5. What works (verified 2026-08-14)

- SWS sync, reliably, via the patched `activate()` (stream-then-release race +
  retry loop).
- Chip clock **≈25.3 MHz** — let the AUTO sweep find it. **Never pass `-c 48`**;
  that number came from a discredited measurement and fails 80/80.
- Flash reads return correct data. Offset 0 is genuine Telink firmware:
  `41 c0 ...` TC32 reset vector, **`KNLT` magic at 0x08** (`TLNK`, byte-swapped).
- One fully clean run: 8 KB at `SWS_RDSIZE=0x40`, zero errors, 38 s.
- **Watchdog disable was the breakthrough** (320 bytes → 25 KB). Halting the CPU
  means nothing feeds the hardware watchdog; it fires seconds later, resets the
  chip, and firmware reclaims the SWS pin. Fix in `activate()`:
  `sws_wr_addr_usbcom(serialPort, 0x622, bytearray([0x00]))` — `reg_tmr_ctrl` is
  32-bit at `0x620`, `FLD_TMR_WD_EN = BIT(19)` = bit 3 of the byte at `0x622`.

Retrieved so far, in `dump_partial/`: `seg_00.bin` (25344 B from offset 0,
header verified), `seg_01.bin` (6144 B @ 0x10000), `seg_02.bin` (12288 B @
0x20000).

## 6. Open problem → `DEBUG_BRIEF.md`

Reads stall and do not recover. `seg_00` fails at **exactly 0x6300 on 3/3
attempts**, which looks reproducible and address-linked and therefore does NOT
fit a pure elapsed-time watchdog story. Both explanations are live and they
conflict; resolving that is step one. Full data tables, ranked hypotheses, and
the experiments that settle it are in the brief.

## 7. Traps — each cost hours. Do not repeat.

- **pyserial ASSERTS DTR ON PORT OPEN.** With DTR/GPIO wired to RESETB, the chip
  sits in reset and **clamps the shared SWS line**, killing the echo — which
  looks exactly like a broken wire. Any new script MUST call `setDTR(False)`
  right after opening. This produced an entire session of phantom "the wiring is
  broken" results; scripts that released DTR worked fine all along.
- **Echo contamination gives false positives.** TX and RX share one wire.
  `reset_input_buffer()` immediately after `write()` clears NOTHING (bytes are
  still in flight), and the echo then reads as "chip data". **The only valid
  liveness test: did bytes arrive that we did not send** (`sws_verify.py` proves
  `in_waiting == 0` first).
- **A swsdiv round-trip check is self-fulfilling** — the value verified is itself
  encoded in the frame just sent. Tell-tale: swsdiv reported 104, 89, 89, 64
  across runs when a real chip clock is FIXED. Source of the bogus "48 MHz".
- **A Phase A echo test cannot validate the pin-17 joint.** The rig forks: one
  wire to pin 17, splitting into TX-via-1k and RX. The echo loops back at the
  fork and never traverses the pad.
- **`fuser -v <port>` before every run.** A stale script holding the port (Linux
  does not lock ttys) silently corrupts everything — a leftover 1 Hz DTR toggler
  cost a whole session.
- **Reset wire on pin 16 (TXD) instead of pin 1** cost a session: the 19→11 mA
  drop was misread as "reset works" when it was really the module's UART TX
  being held low.
- **Never `wr_usbcom_blk()` in the activation race** — it calls `flush()`, which
  blocks until bytes are gone, emptying the pipe and defeating the race. Raw
  `write()` only queues.
- Zero-pad segment filenames or `seg_10` sorts before `seg_2`.

## 8. Files

```
DEBUG_BRIEF.md        the open problem, self-contained
PI_SWIRE_SETUP.md     Pi rig: UART config, wiring, watchdog, run procedure
HANDOFF.md            this file
dump_partial/         retrieved firmware bytes + the patched flasher
TlsrComSwireWriter/   pvvx clone, patched. Stock at TLSR825xComFlasher.py.orig
  pi_run.py           runs any tool with setDTR() redirected to a GPIO
  sws_verify.py       echo-immune liveness probe — the trustworthy test
  dtr_polarity.py     proves which DTR state frees the SWS line
  sws_race.py sws_powercycle.py rx_hunt.py tx_alive.py loopback_test.py
zt3l_docs/            datasheet drawings (authoritative pinout source)
```

Note: this folder is **not under git**. Nothing here is version-controlled.
