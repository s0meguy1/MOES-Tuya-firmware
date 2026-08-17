# MOES / ZT3L firmware research

> **Work in progress — do not flash this firmware.** This repository contains
> the replacement firmware and the research used to diagnose a device-silence
> failure. The fix is still being developed and has not been proven on hardware.

This repository is an **experimental research workspace** for a Tuya ZT3L
(TLSR8258) Zigbee module.  It is not a production-ready firmware release and
contains no image that is approved for deployment.  Use only on hardware you
can recover with a dedicated programmer.

## Public status

- The flash layout is statically supported by the retained evidence: app at
  `0x8000`, OTA staging at `0x70000`, install descriptor at `0xF7000`, and NV
  at `0xD8000`.
- Build 12 has the corrected descriptor encoding but is only a version bump
  over build 11.  It has not been silicon-tested.  An update from one custom
  build to another remains unproven.
- A recurring radio/firmware wedge has not been root-caused or fixed.
- SWire reads have a known artifact model and need repeated reads plus merge
  verification for an exact dump.  Observed SWire writes corrupt data; there
  is no validated wired write path.
- The UART helper is a **non-destructive probe**.  It does not send OTA START;
  a silent probe is inconclusive and does not establish UART OTA availability.
  On a Pi it requires an external wrapper that maps serial DTR to the reset
  GPIO; `/dev/ttyAMA0` does not provide a reset control line by itself.

The public-safe technical status matrix is maintained in the separately
versioned companion firmware checkout at `bughunt/VERIFICATION_STATUS.md`.
That checkout's README has the branch-specific deployment warning.

## Safe tooling

`dump/TLSR825xComFlasher.working.py`, `dump/merge_verify.py`, and
`dump/swire_diag.py` are research tools.  Treat hardware interaction as
read-only unless a separate, reviewed procedure says otherwise.  The historical
capture/flash shell helpers are deliberately disabled and cannot touch
hardware.

## Privacy and repository history

Local handoffs, raw bench captures, firmware readouts, logs, and hardware
specific helpers are ignored.  A normal removal from the current Git index
does **not** erase material already present in published history.  Do not
assume a future cleanup commit purges prior captures; history rewriting or
credential rotation requires explicit owner approval.

`HANDOVER_2026-08-17.md` is a detailed, ignored local handoff.  It is the
authoritative operational record for the current bench; it deliberately is not
public release documentation.
