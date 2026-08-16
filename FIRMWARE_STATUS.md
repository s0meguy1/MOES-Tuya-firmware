# Moes TS0505B custom firmware — status & runbook

Working tree: `work/tuyaZigbee` (fork of doctor64/tuyaZigbee), branch `moes-ts0505b`.
Fleet: 46 × Moes ZB-TDD6-RCW-4, Zigbee `TS0505B` / `_TZ3210_b8jdosxo`,
Tuya ZT3L module = Telink TLSR8258, 1 MB flash, on zigbee2mqtt 2.11
(docker on `<z2m-host>`, data dir `<z2m-data>`).

---

## 0. Corrections — 2026-08-16 (read first; supersedes stale claims below)

Established 2026-08-16 by byte-level SWire reads of the bench unit. z2m host
logs are **EDT (UTC-4)**; UTC is used here.

* **Build 06 has been flashed** to the bench unit (the stock→ours conversion
  OTA completed 2026-08-15 and it joined). §2's "not been flashed to any device
  yet" is stale.
* **Build 07 has been transferred but never installed.** The ours→ours OTA
  download completed 14:27 UTC and the unit rebooted 14:29:30 UTC, but the
  bootloader never installed it: app slot `0x8000` still runs build 06
  (version `03 30 06 11`); build 07 (version `03 30 07 11`) sits byte-intact
  and CRC-valid at the `0x70000` staging bank with install-flag `0x4b` present.
  The earlier "`installed_version 285683715` = build 07 runs" claim was wrong —
  z2m's `installed_version` is optimistic bookkeeping. Root cause:
  `ota_mcuReboot()` gates `SYSTEM_RESET()` on `flash_writeWithCheck()` returning
  TRUE, and the verify can report FALSE after the write has already landed, so
  no reset → no install. Full analysis: `work/tuyaZigbee/bughunt/ota_no_install.md`.
* **The liveness chain is unproven on hardware.** Build 07 has never run
  anywhere; "hardware observation pending" is superseded by "never installed".
  Design risk in `work/tuyaZigbee/bughunt/liveness_no_fire.md`: the liveness
  sampler shares the cooperative `ev_timer` list the wedge starves, so the
  build-07 chain may not fire on a wedge. Hardware observation will confirm.
* **New wedge flavor.** The unit wedged again ~14:36–14:37 UTC in a
  joined-but-silent form (`joined=1`, radio RX, no scan active, rescue stable
  timer frozen at 13 min) — distinct from the build-06 parent-loss scan freeze.
* **"Traffic suppresses the wedge" now has a mechanism.** See
  `work/tuyaZigbee/bughunt/idle_parent_loss.md`: neighbor age is reset only by
  inbound link-status RX; the device TXes link-status every 15 s; inbound app
  data refreshes parent lqi, not age. Some steps are INFERRED — check the doc's
  own labels.
* **Operational: the bench unit's staging bank is PRIMED** (flag `0x4b` +
  CRC-valid build 07), so the **next reset/power-cycle will install build 07**
  via the bootloader — plan around that. *(Superseded by §0b: the missing
  `0xF7000` descriptor meant the bootloader never installed it — the primed bank
  is stale.)*
* **Build 08** (ota_mcuReboot unconditional-reset fix) is being prepared
  concurrently; its contents may still change. *(Superseded by build 09 — see
  §0b.)*

---

## 0b. Closeout addendum — 2026-08-16 ~23:00 UTC (build 09)

**Build 09 exists and is Gate-A green, uncommitted** on `moes-ts0505b`
(HEAD `87a4e2d` = build 07). Tracked edits: `common/version.h`,
`MOES_EDITING_GUIDE.md` (+2 SDK-patch rows), `light/moes_liveness.{c,h}`,
`light/moes_rescue.{c,h}`, `light/zb_appCb.c`, `tools/rescue_hosttest/**`. The
OTA fix lives in the vendored, gitignored `build/tl_zigbee_sdk/zigbee/ota/ota.c`.
`APP_BUILD 0x09`; host tests **27/27**. Image
`6464-0395-11093003-light_TS0505B.zigbee` (202450 B, sha256
`54bdd8919b2d39e226d8c874217fc5bce2594cef85289b0884faa845c731149f`), served at
`http://<ota-server>:8094/`.

**Headline root cause (supersedes §0's build-07 no-install bullets):** the real
Tuya bootloader is a **5-state install machine** (`bughunt/install_sm.md`), not
the SDK `bootLoader` sample. It reads a **12-byte descriptor at `0xF7000`**
first, then the single-byte `0x4B` staging flag second. The **stock app writes
that descriptor** in its own OTA-complete path; our SDK firmware never did. So
ours→ours OTA has **never** been able to install — the true mechanism behind the
"updates once" curse. Build 09's `ota_mcuReboot()` now writes the `0x4B` flag,
writes the 12-byte descriptor `{0x70001, size, 1}` at `0xF7000`, and resets
unconditionally.

**Chicken-and-egg / fleet path.** An already-converted unit (bench, build 06)
cannot install anything OTA anymore: no descriptor writer on the device, SWire
writes broken, UART protocol unanswered. A STOCK unit always works (stock writes
the descriptor). Clean path: **stock → build 09 conversion OTA → ours→ours
forever after** (build 09 writes descriptors). The bench unit is therefore
**STRANDED on build 06 remotely** — wired recovery only.

**Operational footgun.** `tools/tl_check_fw.py` is a **PATCHER, not a checker**.
Running it on a `.zigbee` mangles the OTA header in place (it is for raw Telink
`.bin` only); a Gate-A run had to rebuild the image after it rewrote one. Do not
run it on a served `.zigbee`.

**OTA stall state.** `genOta.fileOffset 131250` / `imageUpgradeStatus 1` sits in
the bench unit's NV from the build-07 transfer era — stale, not a live transfer.

**Day's hardware saga (UTC, condensed — full detail in HANDOFF.md):** build 07
downloaded 14:08–14:27 but never installed (no descriptor + gated reset);
class-2 wedge ~14:36; 17:20 soft reset booted it but it wedged again ~20 min
later with the rescue stable clock **clearing** probation during the wedge
(net-rewards-wedge, fixed in build 09); 17:41–17:47 SWire reads left it halted;
the chip sat **halted ~4 hours** while ~19 "resets" did nothing because the
GPIO17 RST wire died between 17:47 and 19:16 (lesson: byte-identical SRAM across
"resets" means the resets are not firing — validate state CHANGES after a reset);
22:05 a remote `SYSTEM_RESET` via SWire register write (`mw 0x6f = 0x20`, new
capability) booted it for real, then it wedged silently and the app reclaimed the
SWS pin; bench supply is manual; 22:23 a raw ZCL factory-reset command was
delivered and ACKed (TSN 52) but never executed (deferred action starved with the
timer list). **No remote reset paths remain.**

---

## 1. What is PROVEN (observed, not inferred)

| Fact | Evidence |
|---|---|
| Stock firmware **accepts our unsigned, plaintext image** over standard Zigbee OTA | two devices began downloading it and wrote it to flash |
| Stock queries OTA with **manufacturer `0x1141`, imageType `0xD3A3`** | z2m matched our file on those values |
| Stock reports **fileVersion 101**; `0xFFFFFFFF` is accepted as newer | z2m offered and the device took it |
| **No encryption / no signature** anywhere in the path | plain element tag `0x0000`, image accepted |
| The radio's IEEE comes from **Tuya's ASCII EUI-64 at `0x0FB058`**, not the binary block at `0x0FF000` | bench light joined as `0xa4c138…eccd`, exactly the decoded ASCII value |
| A **failed/stalled download is a no-op** — device keeps running stock | observed 3×, device healthy and controllable afterwards |
| Toolchain builds a byte-valid image | KNLT magic, `5d02`, size field, CRC32 all verified programmatically |

## 2. What is NOT yet proven

**Correction (2026-08-16):** the first bullet is stale — build 06 *has* been
flashed (conversion OTA completed, unit joined) and build 07 *has* been
transferred but never installed. See §0.

* **That our firmware boots.** Build 06 is the current candidate and has **not
  been flashed to any device yet**; no transfer has completed yet.
* Colour/channel correctness on real LEDs.
* The effect engine end-to-end.
* A second OTA *from* our firmware (the thing that fixes doctor64's #23).

Build 06 changes none of the above until it actually boots on a bench unit.

---

## 3. Flash map (the thing that matters most)

Derived from the stock dump and the stock bootloader's behaviour:

```
0x000000  stock Tuya bootloader          KEEP - never erased
0x008000  application (ours, ~196 KB)
0x070000  OTA staging bank               <- what the stock bootloader reads
0x0D8000  NV base (ours = Tuya's)        NV spans to 0x0EE000
0x0F8000  Tuya board-config JSON         per-model   - do not erase
0x0FB000  Tuya identity + ASCII IEEE     PER-DEVICE  - do not erase, unique
0x0FF000  binary address block           per-device, NOT the live IEEE
```

`MOES_NV_BASE_ADDRESS=0xD8000` is the single most important build flag:

* SDK default in bootloader mode is `0xE6000`, which puts the keypair NV
  module at `0xF4000–0xFC000` — **on top of the factory config and the
  IEEE**. Joining a network would erase the light's MAC permanently.
* `0xD8000` also makes `FLASH_ADDR_OF_OTA_IMAGE` land exactly on `0x70000`,
  which is where the stock bootloader looks. The SDK default `0x77000` is
  the root cause of doctor64/tuyaZigbee#23 ("updates once, then never again").

## 4. Bugs found in pre-flight review (all fixed)

1. **NV over factory data** — would erase the IEEE. Fatal, unrecoverable.
2. **OTA bank `0x77000` vs `0x70000`** — updates would work exactly once.
3. **PWM duty `/2`** — every channel capped at 50 % brightness.
4. **Config parser wrote 17/18 fields to wrong offsets** — all five pin
   fields collided on one offset. Runtime parsing removed entirely; the
   pin map is compiled in and verified two independent ways.
5. **Reset-skip left a stale power-cycle count** — could trip the 3-cycle
   factory reset after an update.
6. **Tuya `0xEF00` frame parsed with a u8 seq** — it is u16, so every field
   was read one byte early.
7. **`0xEF00` registered as manufacturer-specific (0x1002)** — herdsman
   sends that cluster with *no* manufacturer code, so the SDK would have
   rejected every effect command. Now `MANUFACTURER_CODE_NONE`.
8. **Implicit declarations** of `hsvToRGB` / `moes_outSet` in the effect
   engine (missing prototypes).

## 5. Build

```bash
cd work/tuyaZigbee
cmake . -B build -DDEVICE_VARIANT=TS0505B
cmake --build build --target light_TS0505B.zigbee -j8

# conversion image (served to lights still running STOCK firmware)
python3 tools/make_ota.py -c 0x1141 -t 0xD3A3 -v 0xFFFFFFFF \
        -ot moes-conv-v1 build/light/light_TS0505B.bin
```

Two artifacts:

* `1141-d3a3-ffffffff-moes-conv-v1.zigbee` — **stock → ours** (first hop)
* `6464-0395-<ver>-light_TS0505B.zigbee` — **ours → ours** (all later updates)

### 5.1 Build 07 — current HEAD, transferred but not installed

Repo `work/tuyaZigbee`, branch `moes-ts0505b`. **Correction (2026-08-16):
build 07 has been transferred (OTA download completed 14:27 UTC) but never
installed** — the app slot still runs build 06, and the staging bank is primed
(flag `0x4b` + CRC-valid build 07), so the next reset installs it. See §0.

Build 06 bench outcome (2026-08-15/16): the stock→ours conversion OTA
completed and the unit joined, but ~70–95 s after every join the nwk layer
declares its parent lost, starts a rejoin scan, and the scan freezes inside
the prebuilt MAC lib (`bughunt/mac_scan_wedge.md`). No callback ever fires
again, the main loop keeps feeding the watchdog, and the probation counter
never advances — the build-06 safety net cannot engage on this wedge class.

In build 07:

* **rejoin-scan liveness monitor** (`light/moes_liveness.c`): once a boot has
  network credentials, "continuously unjoined (`zb_isDeviceJoinedNwk()==0`,
  verified 0 in both hang captures) **and** zero BDB commissioning events for
  60 s" forces a marked `SYSTEM_RESET()`. A wedged scan produces exactly that
  signature; a live stack that merely can't find its coordinator keeps
  raising `REJOIN_FAILURE`, which holds the fuse open — no fleet-wide reset
  loop on coordinator outage. Pairing devices are never armed.
* Each forced boot increments probation → a repeating wedge latches **rescue
  mode on boot 7** (~15 min) instead of bricking; rescue boots keep the
  monitor armed, so rescue cannot re-brick either.
* Host test now 22/22, including the full
  wedge→reset→probation→rescue→healthy-clear chain driven by the real
  modules (`tools/rescue_hosttest`).

Bench gate before any ceiling light, in order:

1. ~~Pi SWire read backup first~~ — **DONE 2026-08-15**: two full dumps in
   `dump/bench_2026-08-15/bench_full_{1,2}.bin`. Unit confirmed **stock**
   then; it has since been converted and currently carries build 06 (wedged
   again ~14:36 UTC in a new joined-but-silent flavor — see §0). Working read
   config:
   `-b 460800`, `SWS_DIV=110`, `-t 200`, `SWS_RDSIZE=0x100`.
2. Serve the workstation-built image over HTTP with `sha256` verified against
   the build artefact.
3. Power-cycle the bench unit. Its staging bank is already primed (flag
   `0x4b` + CRC-valid build 07), so the stock bootloader installs build 07 on
   the next reset — no further OTA needed (the download completed 14:27 UTC).
4. Let it wedge on build 07 and **watch whether it saves itself**: forced
   reset at ~60 s of silence, boot-time rejoin, wedge repeats, rescue latches
   on boot 7. SWire-read the rescue state (`s_failCnt`/`s_rescue`) to prove
   the chain on real hardware. *(2026-08-16 caveat:
   `bughunt/liveness_no_fire.md` predicts the liveness fuse may not fire on a
   wedge — this step is now a test, not an expectation.)*
5. 30 min soak + light-show blast + CCT commands, then one ceiling fixture
   with the announce watcher running.

## 6. Pushing an update (per device, never fleet-wide by accident)

Image is served over plain HTTP from the workstation:
`http://<ota-server>:8094/…` (`python3 -m http.server 8094` in
`work/tuyaZigbee/build/light`). Port `8093` is stale — never use it.

```bash
mosquitto_pub -t zigbee2mqtt/bridge/request/device/ota_update/update \
  -m '{"id":"0x<ieee>","url":"http://<ota-server>:8094/<file>.zigbee"}'
```

The per-device `url` form is deliberate: it targets exactly one device and
never publishes an index that the other 45 could match.

z2m needs `ota: true` on the definition or it refuses outright — supplied by
`data/external_converters/moes_ts0505b_ota.js`, which clones z2m's own
built-in definition and changes only that one field (plus a fingerprint
restricted to `_TZ3210_b8jdosxo`). `converters/moes_ts0505b_ota.js.withfx`
is the same thing plus the `light_show*` controls, for after conversion.

## 7. Known transport problem

OTA transfers stall partway (bench: 2.81 %, then 12.44 %). The device stops
requesting blocks; no reboot, no error, healthy afterwards. z2m already uses
a conservative 50 B / 250 ms. Untried remedies:

* raise `ota.image_block_response_delay` (500–800 ms)
* retry — a power cycle restored the bench light's LQI from 167 to 255 and
  it then got 4× further

Because a stall is harmless, retrying is cheap.

## 8. Wired recovery — read before you need it

**SWire writes are unusable on this part.** Byte values `0x80–0xBF` are
corrupted deterministically in *both* flash and SRAM writes, independent of
divider, chunking and cell encoding. Cause: a UART-framed `'1'` cell is
8/10 bits low, leaving a 2-bit-time gap the chip's decoder swallows, so a
`'0'` after `'1'`s decodes as `'1'`. This is why pvvx abandoned COM-port
SWire writing. **SWire reads are perfect** (`SWS_DIV=110`, `-b 460800`) and
remain the way to back a unit up.

The stock bootloader's UART OTA protocol (module pins 15/16, 115200,
`0x55`/`0xAA` framing, crc8) is implemented in `uart_flash/uart_flash.py`
but the module never answered on those pins — unresolved.
