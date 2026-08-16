# Raspberry Pi as the ZT3L SWire programmer

Why this beats the CP2102 rig: the Pi's **PL011 is an on-chip UART**. There is no
USB packet scheduling between the CPU and the wire, and reset is a real GPIO with
real timing. That is the same property that makes a TB-03F-KIT work — it is not
the brand, it is that a microcontroller generates the timing instead of USB.

---

## 1. Use the PL011, never the mini-UART

The Pi has two UARTs on GPIO14/15:

| Device | Which | Use it? |
|---|---|---|
| `/dev/ttyAMA0` | PL011, own clock | **YES** |
| `/dev/ttyS0` | mini-UART | **NO** |

The mini-UART derives its baud from the VPU core clock, which moves with CPU
frequency scaling — the baud drifts under load. That is exactly the jitter we
are trying to escape, so getting this wrong defeats the whole point.

### Pi 3 / 4 / Zero W / Zero 2W

Bluetooth owns the PL011 by default. Free it:

```bash
sudo raspi-config
#   Interface Options -> Serial Port
#     "login shell accessible over serial?"  -> NO
#     "serial port hardware enabled?"        -> YES
```

Then add to `/boot/firmware/config.txt` (Bookworm) or `/boot/config.txt` (older):

```
dtoverlay=disable-bt
```

```bash
sudo systemctl disable --now hciuart
sudo reboot
```

### Pi 5 — VERIFIED 2026-08-14 on the LiDar Pi (Trixie)

The header UART is **NOT on by default**. Out of the box `/dev/ttyAMA0` does not
exist at all and `pinctrl get 14,15` reports `none` — those pins are plain
GPIOs, not UART. Enable it:

```bash
echo 'dtparam=uart0=on' | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

**`/dev/serial0` is the WRONG thing to check on a Pi 5.** It aliases
`ttyAMA10`, which is the dedicated 3-pin *debug connector* — a different
physical header from the 40-pin strip, and it stays pointing there even after
the header UART is enabled. Use `/dev/ttyAMA0` explicitly.

### Verify before wiring anything

```bash
ls -l /dev/ttyAMA0                 # must EXIST
pinctrl get 14,15                  # must read TXD0 / RXD0, not "none"
systemctl is-active serial-getty@ttyAMA0.service   # must be inactive
```

Known-good output:

```
14: a4    pn | hi // GPIO14 = TXD0
15: a4    pu | hi // GPIO15 = RXD0
```

On Pi 3/4/Zero W instead confirm `ls -l /dev/serial0` resolves to `ttyAMA0`,
NOT `ttyS0`. If it points at `ttyS0` the overlay did not take — fix that first.

---

## 2. Wiring

**Mains stays disconnected. Always.** The driver's low-voltage rail is not
isolated.

| Pi (BCM / header pin) | → | ZT3L |
|---|---|---|
| GPIO14 TXD (pin 8) | → **1 kΩ** → | pin 17 SWS |
| GPIO15 RXD (pin 10) | → direct → | pin 17 SWS (same pad) |
| GPIO17 (pin 11) | → | pin 1 RST |
| GND (pin 6) | → | pin 9 GND |
| 3V3 (pin 1) | → | pin 8 3V3 |

Same SWire topology as before: **the resistor goes in the TX leg only**, and RX
taps the pad side of it. Pad map: HANDOFF.md §3 (drawings in `zt3l_docs/`;
the annotated photos were lost in the 2026-08-14 cleanup).

**Power the module from the Pi's 3V3 (pin 1) and leave the bench supply
disconnected.** The module draws ~19 mA, nothing for that rail, and it makes
ground inherently common — which kills off the "is my ground actually common?"
question that cost hours on the CP2102 rig. Keep exactly one power source.

Trade-off: you lose the bench supply's current display as a chip-state probe
(~19 mA running / ~11 mA held in reset). If you want that back, use the bench
supply instead and tie **Pi GND ↔ bench GND ↔ module GND** into one node.

---

## 3. Software

```bash
sudo apt install -y python3-serial python3-gpiozero python3-lgpio
# from the desktop, push the already-patched toolkit:
rsync -av <project-root>/TlsrComSwireWriter/ <PI>:~/tlsr/
```

Copy the whole directory — the flasher there is **already patched** with the
stream-then-release race and the 80-attempt retry loop, and the diagnostics
carry the hard-won echo discipline.

`pi_run.py` redirects `setDTR()` onto the GPIO and runs any tool unmodified, so
nothing else needs editing. Reset GPIO defaults to BCM 17; override with
`RST_GPIO=<n>`.

---

## 4. Run order

**a. Prove the line, before anything else.** This is the echo-immune test — it
only asks "did bytes arrive that we did not send":

```bash
cd ~/tlsr
python3 pi_run.py sws_verify.py /dev/ttyAMA0 921600
```

- **1 byte (`fe`) every trial** → chip silent. Expected on the old CP2102 rig;
  on the Pi this should NOT happen — it did answer on 2026-08-14. Suspect the
  wiring or the pin-17 joint (HANDOFF §3, §7).
- **More than 1 byte** → **the chip is alive.** Straight to (b).

**b. Test read, 256 bytes — USE THE AUTO CLOCK SWEEP:**

```bash
python3 pi_run.py TLSR825xComFlasher.py -p /dev/ttyAMA0 -t 200 rf 0 0x100 t.bin
```

**Do NOT pass `-c 48`.** It fails on this chip (80/80 races). The 48 MHz figure
came from a false-positive swsdiv reading on the CP2102 rig and was simply
wrong; the auto sweep finds the real clock and syncs in seconds.

A good dump starts like this — `KNLT` at offset 0x08 is the Telink firmware
magic (`TLNK`, byte-swapped), `41 c0` is the TC32 reset-vector jump:

```
000000 41 c0 00 00 00 00 00 00 4b 4e 4c 54 00 08 c8 00  >A.......KNLT....<
```

**c. Full dump — twice, then compare:**

```bash
python3 pi_run.py TLSR825xComFlasher.py -p /dev/ttyAMA0 -t 200 rf 0 0x100000 zt3l_full_1.bin
python3 pi_run.py TLSR825xComFlasher.py -p /dev/ttyAMA0 -t 200 rf 0 0x100000 zt3l_full_2.bin
sha256sum zt3l_full_*.bin && cmp zt3l_full_1.bin zt3l_full_2.bin && echo "IDENTICAL"
```

Do not trust a dump that does not reproduce byte-for-byte. Keep it safe — it is
also the restore image (`wf 0 zt3l_full_1.bin`).

---

## 4b. THE WATCHDOG — the thing that actually blocked the dump

Sync was never the last hurdle. Once SWS answered, reads worked for ~5 chunks
and then failed forever, no matter how many retries.

**Cause:** halting the CPU (`[0x0602]=0x05`) means nothing feeds the hardware
watchdog. It fires a second or two later, resets the chip, and the firmware
takes the SWS pin straight back.

This masquerades as flaky hardware. Measured at offset 0x100: chunk size
`0x40` OK, `0x80` FAIL, `0xc0` OK — which looks random but is really about how
much *elapsed time* each read consumes before the watchdog fires. It also looks
address-specific (0x100 "always" failed while 0x8000 worked) purely because of
where the timer happened to land.

**Fix, patched into `activate()`:**

```python
sws_wr_addr_usbcom(serialPort, 0x622, bytearray([0x00]))
```

`reg_tmr_ctrl` is 32-bit at `0x620`; `FLD_TMR_WD_EN` is `BIT(19)` = bit 3 of the
byte at `0x622`. The CPU is halted at that point, so zeroing the timer byte is
safe. Result: 8 KB read with **zero** failures where 320 bytes had been the
ceiling.

Chunk size is `SWS_RDSIZE` (default `0x40`; `0x100` is ~4x faster and fine with
the watchdog disabled).

**Prefer segmented dumps.** One monolithic 1 MB read still stalled at 0x6300;
16 x 64 KB segments with per-segment retry make progress durable:

```bash
for i in $(seq 0 15); do
  off=$(printf "0x%x" $((i*65536))); nm=$(printf "seg_%02d.bin" $i)
  for try in 1 2 3; do
    SWS_RDSIZE=0x100 python3 pi_run.py TLSR825xComFlasher.py \
      -p /dev/ttyAMA0 -t 200 rf $off 0x10000 $nm >/dev/null 2>&1
    [ "$(stat -c%s $nm)" = "65536" ] && break
  done
done
cat seg_??.bin > zt3l_full_1.bin      # zero-padded names: seg_10 must not sort before seg_2
```

## 5. Pi-specific gotchas

- **`ttyS0` instead of `ttyAMA0`** — silent baud drift under CPU load. Check
  `ls -l /dev/serial0` every time.
- **The serial console (getty) will fight you** for the port. Must be disabled.
- **Pi 5 needs `lgpio`/`gpiozero`, not `RPi.GPIO`** — `pi_run.py` handles both
  backends, but `RPi.GPIO` does not work on Pi 5 at all.
- **`fuser -v /dev/ttyAMA0` before every run.** A leftover process holding the
  port cost this project a whole session on the desktop rig.
- Polarity is unchanged from the CP2102: `setDTR(True)` = reset **asserted** =
  pin driven **LOW** (ZT3L pin 1 is RESETB, active low). `pi_run.py` preserves
  this, so every tool behaves identically to the desktop.
- **The GPIO17 RST wire can die silently.** On 2026-08-16 the chip sat CPU-halted
  ~4 hours while ~19 "resets" were issued and produced nothing: SRAM read back
  **byte-identical** across "resets". That signature means **the resets are not
  firing**, not that the chip is wedged. Always validate a reset by confirming
  some state *changed* after it (a re-armed CSMA slot, a fresh stack, a moved
  counter). Re-seat the RST wire (and the known-flaky SWS pin-17 joint,
  `HANG_FINDINGS.md` §9) before trusting any reset.

---

## 6. Remote reset via SWire register write — NEW 2026-08-16

The flasher's `activate()` already writes registers while the CPU is halted (it
zeroes the watchdog byte at `0x622`, §4b), so register writes **work on a halted
chip**. Reuse that to reset the CPU remotely when the RST wire is dead:

```bash
python3 pi_run.py TLSR825xComFlasher.py -p /dev/ttyAMA0 -b 460800 -t 200 mw 0x6f 0x20
```

- `mw <addr> <byte>` writes one byte to an analog/peripheral register.
- `0x6f` = `reg_pwdn_ctrl`; `0x20` = `FLD_PWDN_CTRL_REBOOT` (bit5) = the SDK
  `SYSTEM_RESET()`. This boots the chip through flash `0x0` → the bootloader →
  the app, exactly like a soft reset.
- **One-shot only.** After the boot the firmware reconfigures the SWS pin, so the
  line goes dead again until the next boot window; there is no "hold the chip
  here" state.
- **Only `0x00–0x7F` byte values are trustworthy** over this path — the
  0x80–0xBF write corruption documented in FIRMWARE_STATUS §8 applies to SWire
  writes generally, so `0x20` is safe but larger values are not.
- Verified 2026-08-16 22:05 UTC: this was the reset that finally booted the bench
  unit after the RST wire died.
