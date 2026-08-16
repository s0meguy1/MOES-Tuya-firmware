Bench boothang capture — 2026-08-16 ~18:01–18:10 UTC
===================================================
Unit: Moes TS0505B / Tuya ZT3L (TLSR8258), bench IEEE 0xa4c138…eccd
Chip state: HALTED (SWS halt from the 17:41 UTC decisive SRAM read). Kept halted:
  no --run / no CPU resume, no reset, no flash/SRAM writes to app data.
  All reads were issued with tact=0 (NO `-t` flag) so the flasher's activate()
  reset path was NOT run; the tool synced to the already-halted SWS slave
  directly via set_sws_auto_speed() + SWS_DIV=110. This is the key safety choice:
  the proven `-t 200` read form asserts RESETB, which would destroy the hang.

Pi: <bench-pi>, toolkit ~/tlsr, port /dev/ttyAMA0 @ 460800.
fuser -v /dev/ttyAMA0 -> exit 1 (no holder), nothing killed.
All commands run with `cd ~/tlsr && ... python3 -u pi_run.py TLSR825xComFlasher.py`.

KEY ANSWERS
-----------
1) APP SLOT (0x8000) = BUILD 06.  Header bytes @0x8000:
     58 c0 03 30 06 11 5d 02 4b 4e 4c 54 ...
   version bytes at +2 = 03 30 06 11 -> build 06.  KNLT magic intact.
   -> Build 07 was NOT installed by the 17:20 reset.

2) STAGING (0x70000) = INTACT, still primed with build 07.
   Header bytes @0x70000:
     58 c0 03 30 07 11 5d 02 4b 4e 4c 54 ...
   version at +2 = 03 30 07 11 -> build 07.  Byte @0x70008 = 0x4b ('K' of KNLT)
   still set (not 0xFF).  -> staging NOT consumed; install did NOT happen.

FILES (source addr / size, capture UTC, read command)
-----------------------------------------------------
All logs are the raw stdout/stderr of the read tool (no timestamps inside the
tool itself; times below come from the session clock).

app8000.bin       flash rf 0x8000  0x100  ~18:01:45 UTC
app8000.log
  cmd: SWS_RDSIZE=0x100 SWS_DIV=110 python3 -u pi_run.py TLSR825xComFlasher.py \
       -p /dev/ttyAMA0 -b 460800 rf 0x8000 0x100 boothang_2026-08-16/app8000.bin
  result: Read OK 256 B, 0 retries, 0 resyncs.  Worked 3.83 s.

stage70000.bin    flash rf 0x70000 0x200  ~18:02:00 UTC
stage70000.log
  cmd: same, `rf 0x70000 0x200 .../stage70000.bin`
  result: Read OK 512 B, 0 retries, 0 resyncs.  Worked 4.03 s.

sram_pass1.bin    SRAM mr 0x840000 0x10000  18:02:56.500–18:05:28.511 UTC
sram_pass1.log
  cmd: SWS_DIV=110 python3 -u pi_run.py TLSR825xComFlasher.py \
       -p /dev/ttyAMA0 -b 460800 mr 0x840000 0x10000 boothang_2026-08-16/sram_pass1.bin
  result: 65536 B (full 64 KB SRAM).  Worked 151.74 s.

sram_pass2.bin    SRAM mr 0x840000 0x10000  18:07:37.595–18:10:09.334 UTC
sram_pass2.log
  cmd: same, `.../sram_pass2.bin`
  result: 65536 B.  Worked 151.48 s.

rst_regs.bin      SRAM mr 0x60 0x4  ~18:05:40 UTC
rst_regs.log
  cmd: same, `mr 0x60 0x4 .../rst_regs.bin`
  bytes: 7c ff e7 c3   (reset cause; per agent-3 note treat as probably-unreadable)

wd_regs.bin       SRAM mr 0x620 0x20  ~18:05:45 UTC
wd_regs.log
  cmd: same, `mr 0x620 0x20 .../wd_regs.bin`
  bytes: 0x00..0x00 except 0x63e=0x3f.  Byte @0x622 = 0x00 -> FLD_TMR_WD_EN
  (bit 3 of 0x622) = 0 -> watchdog DISABLED (still the state left by the 17:41
  read's activate() watchdog-disable).  Confirms chip can stay halted safely.

PASS-VS-PASS SRAM DIFF
----------------------
sha256 sram_pass1 = 9ebc6705...6fb1
sha256 sram_pass2 = 21e3bf15...d8ec
Byte-level diff: 47 differing bytes out of 65536 (0.072%).  Every one of the 47
is a single-bit toggle of bit 5 (0x20) — the known SWire bit-5 read artifact,
NOT a real state change.  No multi-bit differences.  Conclusion: SRAM did not
change between passes; the chip stayed halted (no re-boot, no liveness motion).

MAC TX-timeout slot @0x84748c (file offset 0x748c) is byte-identical in both
passes:
  f1 1b 02 00 38 06 2a e2 ...
  = callback pointer 0x00021bf1 (mac_waitTxIrqCb, little-endian f1 1b 02 00)
    still armed, deadline bytes 38 06 2a e2 unchanged.

READ ARTIFACTS (per task): bytes 0x80-0xBF bit6-forced, 0xC0-0xDF bit5-forced,
~0.7% bit-5 noise.  Version bytes 03 30 06/07 11 and 0x4b/0xff are
artifact-immune (bits already set/clear), so the build/staging answers above
are reliable.

SESSION NOTE (for provenance): this read session used tact=0 (no reset). The
previous 17:41 session used `-t 200` (activate path) which does assert RESETB
briefly; that is why the pre-reset SRAM capture reflected a fresh boot. This
session preserved that post-reset hang state untouched.
