# Brief: find the hang that bricked fixture #3, and explain the slow OTA

**Date:** 2026-08-15, written for the next AI session.
**You are auditing custom Zigbee firmware for ceiling downlights.** Your job
is analysis and code changes only. A human decides when anything gets
flashed.

Read, in order:

1. `work/tuyaZigbee/INCIDENT_2026-08-15.md` — what happened today, minute by minute
2. `work/tuyaZigbee/AUDIT_FINDINGS.md` — the previous audit (found and fixed a *second* brick-class bug before this one; read "what is left" at the end)
3. `work/tuyaZigbee/FALLBACK_DESIGN.md` — the rescue-mode mechanism, what it covers, and §5 (the watchdog) which this incident proves is the missing half
4. `work/tuyaZigbee/MOES_EDITING_GUIDE.md` — the invariants and *why*
5. `AI_AUDIT_BRIEF.md` — the original brief; §5 (known-good, do-not-fix) and §6 (why there is no wired safety net) still apply in full

---

## 0. Hard rules (unchanged from the original brief)

* **Do not flash anything.** Analysis and code changes only.
* **Do not modify the production host** `<z2m-host>` (SSH works,
  key auth). You may *read* it — its z2m logs and MQTT are the only live
  telemetry. 45 healthy stock lights and a live house depend on it.
* Read-only MQTT only (`zigbee2mqtt/bridge/devices`, `bridge/event`, device
  state). **Never publish** `set`, `ota_update`, or anything fleet-wide.
* Work on branch `moes-ts0505b` in `work/tuyaZigbee`. Commit freely.
* Do not "fix" anything listed in `AI_AUDIT_BRIEF.md` §5, and see §6 below
  for additions.

## 1. Where things stand, in one paragraph

Build 04 (commit `b485043`: the audited tree — key-scanner fix, rescue mode,
reporting sanitizer, and one *behavioural* change: colour-temperature support
via `5d932a6`) was pushed over the air to a healthy stock ceiling light,
`0xa4c138…d282`, at 09:25 today. The 54-minute transfer was flawless.
The bootloader installed it; our firmware booted (drove the warm-white
channel — the light rendered our 2200 K default, so the WW pin is right);
it joined, answered a full z2m configure pass at 10:21:53, and then **hung**
between 10:22 and 10:23: radio dead (`MAC_NO_ACK` continuously), PWM latched
(steady full orange), **no reboots ever** (steady light over 30 s, no dips),
and the hang survived a power cycle. No `device_announce` was seen at any
point — not even on the boot that joined and passed configure. It cannot be
reached over the air. The user is pulling the fixture and will take an SWire
dump. Every other device is untouched and healthy.

**Why this matters more than the last brick:** build 04 contains the
rescue-mode mechanism whose entire purpose was to make this class of failure
recoverable. It never engaged — a hang produces no reboots, and the probation
counter counts reboots. The gap is documented in `FALLBACK_DESIGN.md` §5;
read §5.1 before anything else, because the sequencing argument there is now
field-refuted in one direction: on a device whose only recovery path is OTA,
**a hang is strictly worse than a reset loop**.

## 2. Part A — the hang hunt

The previous audit hunted things that *reset* the device (factory-reset
paths, exception handler, recursion). This incident is the other half of the
fatal class: **code that stops executing forward progress without
resetting**. Hunt for that specifically.

### 2.1 The evidence to explain (all of it, simultaneously)

1. Death window: between 10:21:53 (full configure pass answered — reads,
   binds, reporting writes all worked) and 10:23:03 (ZDO active-endpoints
   request timed out).
2. z2m's interview had *already* gotten a node-descriptor response — so ZDO
   worked, then stopped.
3. No announce, ever, including on the boot that joined and was configured.
4. No reboots: steady output over 30 s of watching; 25+ minutes of full
   output means the probation counter (which needs 6 boots) never advanced.
5. `MAC_NO_ACK` (0xe9) continuous, occasional `MAC_CHANNEL_ACCESS_FAILURE`
   (0xe1) — the radio is not in RX. A hung CPU with the radio in RX would
   still auto-ACK at the MAC layer in hardware. So the radio was off/idle,
   or the whole chip is stalled.
6. Output = steady full **orange** = warm-white channel at ~full duty, i.e.
   the *last* PWM write was a warm colour. The boot path itself ends with
   the light OFF (`tuyaLight_onOffInit()` → `moes_outSet(0,0,0,0,0)`).
   **Something wrote a colour after boot and before the hang.** Prime
   suspects: Home Assistant restoring state on the fresh join, or a
   colour-temperature command. Ask the user; check HA automations. That
   write matters — it exercises the colour path right before death.
7. It hung with the *colour-temperature code path live* — `5d932a6` was the
   only behavioural change in build 04 and the only code in it that had
   never executed on hardware before today.

### 2.2 Ranked leads

**L1 — the colour change (commit `5d932a6`), specifically
`light/zcl_colorCtrlCb.c`.** Diff it against its parent. The both-mode
branches in `tuyaLight_colorInit()` / `tuyaLight_updateColor()` /
`tuyaLight_colorTimerEvtCb()` and the now-compiled CCT command handlers
(`tuyaLight_moveToColorTemperatureProcess` etc.) are new live code. You are
looking for a *hang*, not a crash: an unbounded loop, a
`scheduled-timer-with-`remainingTime`-frozen` path (note
`light_applyUpdate_16` treats `remainingTime == 0xFFFF` as "never
decrement"), or a lock/ordering interaction with `light_fresh()`'s new
re-entry guard (`light/tuyaLightCtrl.c:314` — the counter must balance on
every path; it does today, but prove it again).
**Differential evidence already exists:** build 05 (commit `a72a2ae`) is
build 04 minus exactly this commit. When a bench unit exists again, 05-vs-04
is a clean A/B.

**L2 — NV / flash-erase during the configure pass.** The death window
contains `zcl_reportingTab_save()` (ZCL NV module write), bind-table saves
(APS NV module), and possibly keypair-sector rotation — all involving
4 KB sector erases. Read `build/tl_zigbee_sdk/proj/drivers/drv_nv.c`
(`nv_flashWriteNewHandler`, the `forceChgSec` path) and
`build/tl_zigbee_sdk/platform/chip_8258/flash.c` for any *unbounded* poll
(e.g. waiting on a flash status bit that never asserts). Also ask: what
happens if an erase runs while the radio ISR needs servicing — how long are
IRQs off, and is there any path where they are never re-enabled
(`drv_disable_irq()` without a matching restore on an error branch)?

**L3 — the prebuilt stack (`libzb_router.a`).** ZDO active-endpoints is
handled inside the library; we cannot read it. But note it answered
node-descriptor first. If L1 and L2 come up clean, the SWire dump is the
only way to see inside.

**L4 — checked and already dead ends (do not re-derive):**
`printf()` busy-waits — `common/zcl_onoffSwitchCfg.c` has live `printf`
calls but the function is never called from the light app (declared in a
header only), and `drv_putchar.c`'s waits are bounded per character anyway.
The key scanner is dead-stripped (symbol-verified). The exception handler
calls `SYSTEM_RESET()` — an exception would have produced reboots, and
there were none.

### 2.3 The no-announce question (answer this regardless)

The 2026-08-14 casualty (v1.1) announced on every cycle — but it was
rejoining an *existing* network. Build 04 did a **fresh join** (empty NV) and
never announced even while joined and answering configure. Determine whether
this SDK announces on first join at all, and if not, say so loudly: **the
announce-cadence abort gate is blind during exactly the conversion window it
was written for.** If that's true, the gate needs a different signal
(responsive reads, or a z2m `device_interview` success event) for
conversions.

### 2.4 The SWire dump (the user is taking it — this is the ground truth)

Procedure is proven: `PI_SWIRE_SETUP.md`, `dump/TLSR825xComFlasher.working.py`,
`SWS_DIV=110`, 460800 baud. Reads work; writes do not (do not try).
When the dump arrives:

* **SRAM** — this is the prize. The stack at the moment of the hang contains
  return addresses; identify the function chain (the ELF from the exact
  build has symbols: `git checkout b485043 && rebuild`, then
  `build/tc32/bin/tc32-elf-addr2line`-style mapping). Note the exact flashed
  bytes are preserved in `work/tuyaZigbee/field-artifacts/` (a rebuild is
  *not* byte-identical — `build_time_str` is baked in).
* **Flash @0x8000** — confirm it matches
  `field-artifacts/1141-d3a3-ffffffff-BUILD04-FLASHED-TO-0xa4c138…d282.zigbee`
  minus its 62-byte OTA header (sha256 `01c148be…`).
* **Flash @0xD8000–0xEE000** (our NV) — look for a half-written sector or a
  torn index in the ZCL/APS modules: direct evidence the hang intersected an
  NV write (would strongly support L2).
* **Flash @0x70000** — the spent staging bank (should be all-`0xFF` after
  install, or hold the old staged image; either is informative).

## 3. Part B — why the OTA took 54 minutes

Measured: 201,570 bytes in 3,235 s ≈ **62 B/s**, zero stalls, lqi 255.
Expected at z2m's documented 50 B / 250 ms: ~200 B/s ≈ 17.5 min. **There is
an unexplained ~3.2× slowdown.** Account for it.

Facts already pinned (do not re-derive):

* The device does *not* self-pace by default:
  `zcl_attr_minBlockPeriod = 0` (`zcl/ota_upgrading/zcl_ota_attr.c:57`).
  With 0, `ota_sendImageBlockReq()` sends the next request immediately
  (`zigbee/ota/ota.c:670`). The server sets the pace.
* The device requests at most `OTA_IMAGE_MAX_DATA_SIZE = 48` bytes per block
  (`zigbee/ota/ota.h:32`; request built at `ota.c:1153-1155`). So 48 B per
  round-trip, and at 250 ms per round-trip the transfer "should" be ~17 min.
* After each request the device waits
  `OTA_MAX_IMAGE_BLOCK_RSP_WAIT_TIME = 5 s` for the response
  (`ota.c:656`) before treating it as lost — only matters if responses are
  slow/lost, which would also show as stalls. There were none.

So the ~775 ms effective per-block period comes from the *server* side.
Check, in order:

1. The z2m host's actual OTA settings — **read-only**:
   `ssh <z2m-host> 'docker exec zigbee2mqtt cat /opt/zigbee2mqtt/data/configuration.yaml'`
   — look for `ota:` → `image_block_response_delay`, and any herdsman
   congestion/backoff settings. Someone may have raised the delay after the
   bench stalls (the file history in `FIRMWARE_STATUS.md` §7 mentions
   500–800 ms being considered).
2. Whether herdsman adds jitter or backs off per-device; the z2m debug log
   during the next (bench) transfer timestamps every block request.
3. Whether z2m honoured the device's `maxDataSize` of 48 or sent less.

Deliverable: a short `OTA_SPEED_ANALYSIS.md` with the measured budget, the
responsible knob, and the recommended change. **Prefer a z2m-side change
(zero firmware risk) over any firmware change.** If firmware changes are
proposed (e.g. larger `OTA_IMAGE_MAX_DATA_SIZE`, or setting
`zcl_attr_minBlockPeriod` to pace deliberately), they go behind a flag and
are bench-tested first — the OTA path is the only recovery path this fleet
has, so nothing that changes it ships untested.

Also worth answering while you're in there: the bench unit's stalls (2.81 %
and 12.44 % on 2026-08-14) never recurred on this healthy ceiling light at
lqi 255. With the pacing explained, say whether the stall signature (device
stops requesting, no error, healthy after) is consistent with the 5 s
response-wait expiring quietly — see `ota_imageBlockRspWait` /
`OTA_EVT` handling at `ota.c:617-660`.

## 4. Part C — build 06 direction (design only; do not ship)

`FALLBACK_DESIGN.md` §5.2 already specifies the precondition: before the
watchdog is ever enabled, add `drv_wd_clear()` inside `ota_newImageValid()`'s
CRC loop (`zigbee/ota/ota.c:138` — it CRCs the whole ~200 KB image in one
call with no watchdog service, and it runs at the most dangerous moment in
the device's life). Then `MODULE_WATCHDOG_ENABLE 1` (`light/app_cfg.h:76`,
600 ms interval, already wired in `apps/common/main.c`). Record the SDK patch
in `MOES_EDITING_GUIDE.md` §2's vendored-patch table. Argue the interval:
600 ms must exceed the worst legitimate IRQ-off section (flash erase under
L2's analysis).

## 5. Repo state and artifacts

* Branch `moes-ts0505b`, HEAD `6ea354f`. Recent history:
  `ea7795b` key-scanner fix → `d0285ce` hardening + rescue mode → `9bc7aef`
  reporting sanitizer → `5d932a6` **colour change (the revert candidate)** →
  `b485043` **build 04, as flashed** → `5274a1f`+`a72a2ae` **build 05
  (04 minus colour)** → `6ea354f` incident record.
* `work/tuyaZigbee/field-artifacts/` — the exact bytes served today, sha256
  recorded in the filenames' commit. **The bench Pi is serving dangerous
  stale images** (`moes-conv-v1` = build 01 = the image that bricked the
  2026-08-14 fixture). Do not use it; do not trust `rollout.py`'s hardcoded
  URLs. If you stage anything, serve from the workstation and verify sha256
  end-to-end, as was done today.
* Build: `cmake . -B build -DDEVICE_VARIANT=TS0505B && cmake --build build
  --target light_TS0505B.zigbee -j8`. Known-benign warnings: the two unused
  colour-loop statics in `zcl_colorCtrlCb.c`. Anything else is a finding.
  Header contract and the full pre-flight checklist: `MOES_EDITING_GUIDE.md`
  §5.
* A host test harness exists for the rescue state machine
  (`tools/rescue_hosttest/`, `make check`, 15 scenarios) and an
  announce-cadence watcher (`tools/announce_watch.py`). Note §2.3 before
  relying on the watcher's gate during a conversion.

## 6. Known-good — do not "fix" (in addition to AI_AUDIT_BRIEF.md §5)

| Thing | Why |
|---|---|
| The colour revert in build 05 | Deliberate differential for the hang hunt. Re-applying `5d932a6` before the hang is understood re-introduces the prime suspect. |
| `MODULE_WATCHDOG_ENABLE 0` in the *shipped* builds until §5.2's precondition lands with it | Sequencing: watchdog only with the `ota_newImageValid()` fix in the same change. |
| Rescue mode's "no NV write on latch" | That is the flash-wear bound; the host test asserts it. |
| `HAVE_NET_BUTTON 0` and both guards | `MOES_EDITING_GUIDE.md` §0.1; symbol-verified dead-stripped in build 04+. |
| Build 04's transfer behaviour | Flawless. The transport is not the problem; don't churn it while hunting the hang. |

## 7. Deliverables

1. `HANG_FINDINGS.md` — every hang-class candidate with file:line, the exact
   causal chain, what in the evidence supports/refutes it, and what the SWire
   dump would show if it were the one. Rank against the 7 evidence points in
   §2.1 — a theory that doesn't explain the orange, or the no-announce, or
   the no-reboots is not the theory.
2. `OTA_SPEED_ANALYSIS.md` — the per-block budget, the responsible knob, a
   recommended change (server-side preferred), and the stall explanation or
   an explicit "cannot determine without a bench transfer".
3. Any unambiguous code fixes, committed with reasoning. Debatable ones stay
   as findings.
4. Updated `FALLBACK_DESIGN.md` §0/§5 and `MOES_EDITING_GUIDE.md` to reflect
   the field result: the hang gap is real, the watchdog is the second half
   of the safety net, and the announce gate is (possibly) blind on fresh
   joins.

## 8. What "done" looks like

A human reads `HANG_FINDINGS.md` and knows what the dump should be compared
against before it arrives — and either one lead clearly explains all seven
evidence points, or the honest output is "the dump must decide, here is what
each outcome would mean". And `OTA_SPEED_ANALYSIS.md` makes the next
54-minute transfer a 17-minute one without touching the fleet's only
recovery path.

If the honest answer to "could the next image do this again?" is yes, say so
plainly. That was true for the last two sessions too, and saying it is what
caught the second brick-class bug before it shipped.
