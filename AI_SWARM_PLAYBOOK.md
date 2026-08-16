# AI Swarm Playbook — Moes TS0505B (TLSR8258) custom firmware

**Audience:** a fresh AI agent (or swarm) with zero context. Your mission:
iterate this firmware — test, debug, rebuild, repeat — until it is a **strong
image**: a light that can never brick itself off the network, so OTA fixes are
always reachable. Then prove it on one live ceiling fixture.

**Read first, in order:** `FIRMWARE_STATUS.md` (runbook), then
`work/tuyaZigbee/HANG_FINDINGS.md` + `work/tuyaZigbee/bughunt/mac_scan_wedge.md`
(the enemy), then `work/tuyaZigbee/MOES_EDITING_GUIDE.md` (editing rules; its
pre-flash checklist is law).

---

## 0. The risk model — why everything below is the way it is

- The lights are ceiling fixtures with **broken SWire writes** (bytes 0x80–0xBF
  corrupt deterministically) and a stock bootloader whose UART never answers.
  If a light leaves the network and can't rejoin, it is a **permanent brick**
  you cannot reach. Fixture #3 died this way (build 04, 2026-08-14).
- Therefore: **nothing flashes a live fixture without the user's explicit
  per-device approval**, and every image must carry the self-recovery net
  (probation + rescue + liveness monitor, see §4).
- The bench unit (`0xa4c138…eccd`, a bare ZT3L on the Pi's bench supply)
  is the only free-fire test subject. It is expendable; ceiling lights are not.

## 1. Environment map

| thing | where | notes |
|---|---|---|
| Firmware repo | `work/tuyaZigbee`, branch `moes-ts0505b` | **build 09 uncommitted** on HEAD `87a4e2d` (HEAD is build 07); vendored SDK + tc32 toolchain in `build/` (gitignored — SDK patches recorded in `MOES_EDITING_GUIDE.md` §2) |
| Host tests | `work/tuyaZigbee/tools/rescue_hosttest` | `make check` — 27 scenarios, must stay green |
| z2m host | `<z2m-host>` | docker `zigbee2mqtt` + `mosquitto` (no auth). **Read-only except explicitly approved actions** |
| Bench Pi | `<bench-pi>` | SWire toolkit `~/tlsr`; passwordless sudo; Bluetooth disabled (PL011 free) |
| OTA server | workstation `<ota-server>:8094` | `python3 -m http.server 8094` already serving `work/tuyaZigbee/build/light` — new builds land there automatically. **Port 8093 has a stale server from an old session; never use it** |
| Bench unit | IEEE `0xa4c138…eccd` | nwk addr **changes on (re)join** — always re-read `bridge/devices`; was 60389, currently 21147 |
| This session's cron | `01M052QZ9RVED3VHKQ0VD2R8Q5` | 3-hourly z2m log check for the bench unit; delete when the bench gate completes |

## 2. Hard safety rules (non-negotiable)

1. Never flash anything without the user's explicit go-ahead for that device.
2. Never fleet-wide MQTT. OTA is per-device with an explicit `url` (§5) so no
   other device can match it.
3. Production z2m host: read-only, except actions the user approves (bounded
   permit-join and single-device OTA gets/updates have been approved before —
   ask each time).
4. `git` mutations (commit/push/reset) only when the user asks.
5. SWire **reads** only. SWire **writes are permanently broken** — do not
   attempt; you cannot unbrick by wire.
6. **Never let anything touch the RST line except Pi GPIO17.** A TX wire
   brushing RST cost a morning: UART traffic machine-gunned the reset pin,
   which the firmware counted as the 3-power-cycle factory-reset gesture, which
   **wiped the network NV** (device went factory-new, needed permit-join to
   return). Rapid resets = factory reset. Treat the reset line as armed.
7. Before any SWire session: `fuser -v /dev/ttyAMA0` — a leftover process
   holding the port once cost a whole session.

## 3. What is proven vs. not (2026-08-16 closeout)

z2m host logs are in **EDT (UTC-4)**; times below are UTC with the EDT log
time in parentheses.

**Proven on hardware:**
- Stock→ours conversion OTA (build 06, ~8 stall-resume cycles, 2026-08-15).
- Ours→ours OTA **transfer** (build 07, 1159 s uninterrupted, downloaded
  14:08–14:27 UTC = 10:08–10:27 EDT). **Install never happened** — see root
  cause below.
- Two wedge classes (build 06): the parent-loss rejoin-scan freeze
  (`bughunt/mac_scan_wedge.md`) and the joined-but-silent APS/NWK/ZCL-report
  hang (`bughunt/boothang_stack.md`).
- Inbound ZCL dispatch works **inside the class-2 wedge**: a raw
  `Reset to Factory Defaults` (cluster `0x0000`, cmd `0x00`) was delivered and
  ACKed 22:23 UTC (TSN 52), but the reset never executed — the deferred action
  is starved with the timer list, consistent with `bughunt/liveness_redesign.md`.
- **OTA no-install root cause (CLOSED by analysis):** the real Tuya bootloader
  is a 5-state install machine (`bughunt/install_sm.md`) that requires a 12-byte
  descriptor at `0xF7000` **in addition to** the `0x4B` staging flag. The stock
  app writes that descriptor; our SDK firmware never did, so ours→ours installs
  have never committed. Build 09 writes the descriptor. (`bughunt/ota_no_install.md`
  and `bootloader_gate_real.md` are the earlier stages; `ota_descriptor.md` was
  superseded — its `0xF7000` search missed the r4 construction.)
- Remote `SYSTEM_RESET` via SWire register write works on a halted chip
  (`mw 0x6f = 0x20`, 22:05 UTC) — documented in PI_SWIRE_SETUP §6.

**Not yet proven (the current frontier):**
- Build 09's install fix and the liveness/rescue redesign have **never run on
  hardware**. Host-tested 27/27. The redesign moves the sampler off the starved
  `ev_timer` list onto a `drv_hwTmr` IRQ and makes the fuse joined-blind
  (`bughunt/liveness_redesign.md`); it addresses the starvation in
  `bughunt/liveness_no_fire.md` and the net-rewards-wedge in
  `bughunt/boothang_stack.md`.
- The "inbound traffic suppresses the wedge" hypothesis has a mechanism analysis
  in `bughunt/idle_parent_loss.md`: neighbor age is reset only by inbound
  link-status RX (the device TXes link-status every 15 s; inbound app data
  refreshes parent lqi, not age). Some steps are INFERRED — check the doc's own
  labels.

**Bench state right now:** the bench unit is **STRANDED on build 06**. It cannot
install anything OTA anymore (no descriptor writer on the device; SWire writes
broken; UART protocol unanswered). The build-07 staging bank is stale. Wired
recovery only — see §9 and HANDOFF.md §0.

## 4. The recovery net (what build 09 carries — keep it, extend it)

- `light/moes_rescue.c` — boot probation: +1 per unstable boot (NV item
  `0x71`); 6 boots → **rescue mode** latches (minimal, OTA-first, no light
  engine). Stable-clear now requires **joined AND continuous progress**, plus a
  1-min confirmation window after the countdown reaches zero — so a class-2
  wedge can never erase probation. Flash wear is bounded by design.
- `light/moes_liveness.c` — two-layer heartbeat: a task-context progress ticker
  on `ev_timer` plus a `drv_hwTmr` `TIMER_IDX_0` IRQ sampler. The fuse is
  **joined-blind**: no scheduler progress for 60 s forces an **unmarked**
  `SYSTEM_RESET()` from the IRQ. This is immune to both wedge classes
  (`bughunt/liveness_redesign.md`). Never armed during pairing.
- Rescue mode queries OTA **immediately on join** (one-shot kick, 1 s delay)
  instead of waiting out the first 10-min interval.
- Hardware watchdog 600 ms + bounded `mspi_wait()` (build 06) — necessary but
  NOT sufficient (the wedge is a logical stall; the main loop keeps feeding
  the watchdog).
- Every deliberate reboot calls `moes_resetSkipNextBoot()` so the
  factory-reset gesture never counts it (MOES_EDITING_GUIDE §4.5). The liveness
  IRQ reset is deliberately unmarked (no NV write from IRQ) and is proven safe
  against the 3-power-cycle gesture by host test.

## 5. Recipe — OTA on the bench unit

### 5.1 Build + verify (every build, no exceptions)

```bash
cd work/tuyaZigbee
cmake . -B build -DDEVICE_VARIANT=TS0505B
cmake --build build --target light_TS0505B.zigbee      -j8   # ours→ours update
cmake --build build --target light_TS0505B.tuya.zigbee -j8   # stock→ours conversion
cd tools/rescue_hosttest && make check                        # 27/27 required
```

Then the pre-flash checklist in `MOES_EDITING_GUIDE.md` §5: zero new warnings
(two benign "defined but not used" in `zcl_colorCtrlCb.c` are expected),
`5d 02`@+6 / `KNLT`@+8 / size field / Telink CRC / identity prefix bytes,
`MOES_NV_BASE_ADDRESS=0xD8000` in the TS0505B compile flags, `APP_BUILD`
bumped (`common/version.h`) or z2m won't offer the image.

**WARNING — `tools/tl_check_fw.py` is a PATCHER, not a checker.** It is for raw
Telink `.bin` only; run on a `.zigbee` it rewrites the OTA header in place and
mangles the served image (a Gate-A run had to rebuild after it did). Never run
it as a "check" on a `.zigbee`.

### 5.2 Serve + fire (per device, explicit URL)

The update image is `build/light/6464-0395-<ver>-light_TS0505B.zigbee`,
already served at `http://<ota-server>:8094/`. Verify reachability:

```bash
ssh <z2m-host> 'curl -sI http://<ota-server>:8094/<file>.zigbee | head -1'
```

Fire (single device only — this is the approved form):

```bash
ssh <z2m-host> 'docker exec mosquitto mosquitto_pub \
  -t zigbee2mqtt/bridge/request/device/ota_update/update \
  -m "{\"id\":\"0xa4c138…eccd\",\"url\":\"http://<ota-server>:8094/<file>.zigbee\"}"'
```

Watch progress in the z2m log (`"progress":NN` publishes). ~17–20 min
uninterrupted. If it stalls or the device wedges mid-transfer: harmless —
`fileOffset` is preserved in NV; re-fire the same command next window and it
resumes. Full observability recipe (raw `genOta.fileOffset` /
`imageUpgradeStatus` reads via `bridge/request/action` raw, decode snippet):
`work/tuyaZigbee/tools/OTA_STALL_PLAYBOOK.md`. **Update `network_address`
from `bridge/devices` first — it changes on every (re)join.**

### 5.3 If the unit is factory-new (no credentials)

It will never answer until you open the network. Bounded permit-join (needs
user approval each time):

```bash
ssh <z2m-host> 'docker exec mosquitto mosquitto_pub \
  -t zigbee2mqtt/bridge/request/permit_join -m "{\"value\":true,\"time\":120}"'
```

A steering device joins within ~30 s. The window then: last traffic → wedge
at ~70–95 s (build 06 behavior; build 07 should self-reset instead).

## 6. Recipe — SWire on the Pi (reads only)

Wiring (PI_SWIRE_SETUP.md §2): Pi pin 8 (TX) →**1 kΩ**→ SWS (module pin 17);
Pi pin 10 (RX) → direct → SWS pad side; Pi pin 11 (GPIO17) → RST (pin 1);
GND pin 6 ↔ pin 9; bench supply 3.3 V → pin 8. Module draws ~19 mA running,
~11 mA held in reset — the bench supply's current display is a state probe.

### 6.1 The proven read combo

```bash
ssh <bench-pi>
cd ~/tlsr
SWS_RDSIZE=0x100 SWS_DIV=110 python3 -u pi_run.py TLSR825xComFlasher.py \
  -p /dev/ttyAMA0 -b 460800 -t 200 rf <addr> <size> out.bin
```

Always `-u` and capture to a file: piped stdout is block-buffered and you
will read an empty log as a hardware failure (we did).

### 6.2 Validate every read

Compare a known region first — the app header at `0x8000` (`KNLT` at +8,
build byte at +4) against a trusted dump (`dump/bench_2026-08-15/hang_capture/app256.bin`).
The known bit-5 SWS sampling artifact flips ~0.4% of bytes (`0xc0`↔`0xe0`) —
tolerable. **Identical bytes across hours or reboots = the read path is
broken, not the device** — SRAM never replays exactly. We hit this syndrome
twice; treat static-forever data as garbage and re-seat wiring before
believing anything.

### 6.3 Watchdog during reads

The flasher's `activate()` already disables the on-chip watchdog (patched —
PI_SWIRE_SETUP §4b); without that, reads die after ~320 B.

### 6.4 Boot-window tools (no reset sync needed)

SWS is only guaranteed alive from power-on until firmware reconfigures the
pin. If reset-sync reads fail, these catch the boot window:

```bash
python3 -u pi_run.py sws_verify.py /dev/ttyAMA0 921600   # line liveness (echo-immune)
python3 -u pi_run.py sws_hunter.py /dev/ttyAMA0          # 25 s boot-window listener
```

Run `sws_hunter.py`, then have the user power-cycle; any `CHIP ACTIVITY`
line proves chip + wire. Full dump procedure (segmented, compare twice):
PI_SWIRE_SETUP.md §4c.

### 6.5 Reading the recovery net's state (build 07 addresses — re-`nm` every build!)

```bash
cd work/tuyaZigbee/build && tc32/bin/tc32-elf-nm light/light_TS0505B | \
  grep -E "s_armed|s_silence|s_timer|s_minsLeft|s_failCnt|s_stableTimer|s_rescue|g_zbNwkCtx|g_macScanParam"
```

build 07: `s_armed/s_silence/s_timer` @ `0x842470/71/74`;
`s_minsLeft/s_failCnt/s_stableTimer/s_rescue` @ `0x842478/79/7c/80`;
`g_macScanParam` @ `0x847480` (channel at +13, state at +16);
`g_zbNwkCtx` @ `0x8474a0` (`joined` = bit 2 of byte +45).
Read `0x842470 0x14` and `0x847480 0x60`. Expected wedge signature:
`joined=0`, scan frozen (channel static across reads), `s_silence` counting.
**These are statics — addresses move every build. Re-`nm` or you will read
garbage and believe it** (we did, twice).

## 7. The iteration loop (swarm orchestration)

1. **Reproduce/characterize** on the bench: z2m log + raw ZCL reads + SWire
   state reads. Every claim needs a hardware observation; "should" is not
   data.
2. **Root-cause on paper first** — write the analysis in `work/tuyaZigbee/bughunt/`
   (see `mac_scan_wedge.md` for the bar: symbolized addresses, register-level
   evidence, refuted theories listed).
3. **Patch minimally** (MOES_EDITING_GUIDE conventions; heavy rationale
   comments; match surrounding style). Good swarm splits: one agent per
   independent bug/analysis; the rescue/liveness/OTA modules are separate
   files and parallelize well. Never let two agents touch `zb_appCb.c`
   concurrently.
4. **Gate A (host):** `tools/rescue_hosttest make check` 27/27 + zero new
   build warnings + pre-flash checklist. A host test exists for every
   recovery behavior — add one for anything new.
5. **Gate B (bench, user-approved) is now blocked:** the bench unit is stranded
   on build 06 and cannot take an OTA (no descriptor writer on the device;
   SWire writes broken; UART unanswered). Recover it wired first (§9,
   HANDOFF.md §0) before running any further image on it. Until then, the first
   stock-unit conversion is the install proof (§8).
6. **Only then** one ceiling fixture, with the announce watcher
   (`tools/announce_watch.py`) running.

Verifying swarm work: the orchestrator re-runs Gate A itself, reviews the
diff (`git diff --stat` + read the hunks), and never reports unverified
claims upward.

## 8. Exit criteria for the STRONG image (live-fixture go/no-go)

The bench unit can no longer prove installs (stranded on build 06, no OTA
path). The **first stock-unit conversion is now the install proof.** Recover the
bench wired first (§9) for the wedge/liveness observations; do the install chain
on the stock ceiling fixture the user approves.

- [ ] Wedge → self-reset within ~2 min → rejoin same IEEE → repeats; rescue
      latches on boot 7 (`s_failCnt` parks at 6, `s_rescue=1` via SWire).
- [ ] Rescue-mode boot joins and answers an OTA query/read inside its window
      (build 09 now queries immediately on join).
- [ ] **Install chain:** stock → build 09 conversion OTA installs (first proof
      the descriptor fix works), then a build 09 → build 09+1 ours→ours OTA
      installs (proves the descriptor writer keeps the path open — the thing
      build 06 never had).
- [ ] 30-min soak with zero unexplained silence; light-show blast + CCT
      commands clean.
- [ ] Coordinator-offline test (z2m down 10 min): no reset loop, rejoins
      cleanly when it returns.
- [ ] Host tests still 27/27+; docs updated (FIRMWARE_STATUS, HANG_FINDINGS).

Then: **one** live fixture, user picks which, announce watcher running,
and a written rollback note (the previous update image URL) in hand.

## 9. Open threads worth a swarm slot

- **CLOSED by analysis + build 09 — OTA no-install root cause (the
  descriptor).** The real Tuya bootloader is a 5-state install machine
  (`bughunt/install_sm.md`) that requires a 12-byte descriptor at `0xF7000`
  **in addition to** the `0x4B` staging flag. The stock app writes it; our SDK
  firmware never did, so ours→ours installs never committed. Build 09's
  `ota_mcuReboot()` writes `{0x70001, size, 1}` at `0xF7000` + the `0x4B` flag
  + an unconditional reset. The earlier `ota_no_install.md` gated-reset finding
  was a *contributing* bug, not the whole story; `ota_descriptor.md` was
  superseded (it missed the r4 `0xF7000` construction). Hardware confirmation
  is pending on the first stock→build-09 conversion.
- **Liveness redesign (CLOSED by design, build 09; hardware pending).** The
  build-07 sampler starved on the `ev_timer` list (`bughunt/liveness_no_fire.md`).
  Build 09 moves the decision to a `drv_hwTmr` `TIMER_IDX_0` IRQ sampler with a
  task-context progress ticker, joined-blind 60 s fuse, and a
  joined+progress+confirm stable-clear (`bughunt/liveness_redesign.md`). It also
  fixes the net-rewards-wedge (`boothang_stack.md`). Needs a hardware
  observation.
- **NEW — bench-unit wired recovery.** The bench unit is stranded on build 06
  and cannot install OTA (no descriptor writer; SWire writes broken; UART
  unanswered). Physical options: retry the UART bootloader protocol on module
  pins 15/16 (`uart_flash/uart_flash.py`), re-seat the dead GPIO17 RST wire and
  the flaky SWS pin-17 joint (`HANG_FINDINGS.md` §9), or swap the ZT3L module.
- **Idle parent-loss trigger** (~70–95 s): the wedge's first domino. Mechanism
  analysis in `bughunt/idle_parent_loss.md` (neighbor age reset only by inbound
  link-status RX; device TXes link-status every 15 s; inbound app data refreshes
  lqi, not age — some steps INFERRED). Candidates: harmless periodic ZCL
  read/keepalive from the device, or root-cause the neighbor-aging path in the
  lib.
- **Light app ignores `BDB_COMMISSION_STA_PARENT_LOST`** (the switch app
  re-issues `zb_rejoinReq` there; `switch/zb_appCb.c:224`). Deferred
  deliberately; revisit with data.
- Root cause of the 2026-08-15 stock-firmware OTA stalls (playbook exists;
  pacing experiments made it worse; default 250 ms was best).

*(Rescue-mode "first OTA query is 10 min out" is now closed: build 09 kicks a
one-shot OTA query immediately on join.)*
