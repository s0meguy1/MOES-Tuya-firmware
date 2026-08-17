#!/usr/bin/env python3
"""Read-only UART-OTA probe for the ZT3L/TLSR8258 bench rig.

The framing below is derived from a Telink SDK bootloader implementation, but
the tested stock bootloader has not answered it.  UART OTA availability is
therefore unconfirmed.  This program's probe only resets and listens; it never
sends an OTA START request.  Silence is inconclusive.

Wire: Pi TXD -> 1k -> ZT3L pin 15 (RXD), Pi RXD <- ZT3L pin 16 (TXD),
RST via the rig's reset GPIO.  A native Pi UART has no DTR/RTS pins, so this
script must run through the local ``pi_run.py``-style wrapper that maps
``setDTR()`` to GPIO17 (or an equivalent reviewed reset adapter).

Usage:
  python3 <path-to-pi_run.py> uart_flash/uart_flash.py -p /dev/ttyAMA0 probe
      reset the chip and listen for framed traffic without sending a command.
      A silent result neither proves nor disproves UART OTA support.
  python3 <path-to-pi_run.py> uart_flash/uart_flash.py -p /dev/ttyAMA0 flash image.bin
      intentionally disabled; this command must not start an OTA transfer.
"""

import argparse
import serial
import struct
import sys
import time

MSG_START_FLAG = 0x55
MSG_END_FLAG = 0xAA

MSG_CMD_OTA_START_REQUEST = 0x0210
MSG_CMD_OTA_START_RESPONSE = 0x8210
MSG_CMD_OTA_BLOCK_RESPONSE = 0x0211
MSG_CMD_OTA_BLOCK_REQUEST = 0x8211
MSG_CMD_OTA_END_STATUS = 0x8212
MSG_CMD_ACKNOWLEDGE = 0x8000

MSG_STA_SUCCESS = 0x00

MSG_BLOCK_REQUEST_INTERVAL = 0.01
RECV_TIMEOUT = 3.0


def crc8_calc(msg_type, length, data):
    c = (msg_type >> 0) & 0xFF
    c ^= (msg_type >> 8) & 0xFF
    c ^= (length >> 0) & 0xFF
    c ^= (length >> 8) & 0xFF
    for b in data:
        c ^= b
    return c & 0xFF


def frame(msg_type, payload=b""):
    crc = crc8_calc(msg_type, len(payload), payload)
    return bytes([MSG_START_FLAG,
                  (msg_type >> 8) & 0xFF, msg_type & 0xFF,
                  (len(payload) >> 8) & 0xFF, len(payload) & 0xFF,
                  crc]) + payload + bytes([MSG_END_FLAG])


class Reader:
    """incremental 0x55..0xAA frame parser"""

    def __init__(self, ser):
        self.ser = ser
        self.buf = b""

    def pump(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.ser.read(256)
            if chunk:
                self.buf += chunk
                while True:
                    msg = self._parse()
                    if msg is None:
                        break
                    yield msg

    def _parse(self):
        while self.buf and self.buf[0] != MSG_START_FLAG:
            self.buf = self.buf[1:]
        if len(self.buf) < 7:
            return None
        mtype = (self.buf[1] << 8) | self.buf[2]
        mlen = (self.buf[3] << 8) | self.buf[4]
        if len(self.buf) < 7 + mlen:
            return None
        crc = self.buf[5]
        payload = self.buf[6:6 + mlen]
        end = self.buf[6 + mlen]
        self.buf = self.buf[7 + mlen:]
        if end != MSG_END_FLAG:
            return None
        if crc != crc8_calc(mtype, mlen, payload):
            return None
        return (mtype, payload)


def open_port(port):
    ser = serial.Serial(port, 115200, timeout=0.05)
    ser.setDTR(False)   # release reset (pi_run.py maps DTR -> reset GPIO)
    ser.setRTS(False)
    ser.reset_input_buffer()
    return ser


def reset_chip(ser):
    """Reset through the rig's reset GPIO. pi_run.py maps the pyserial
    control lines onto GPIOs; assert both, release, as the SWire tools do."""
    ser.setDTR(True)   # via pi_run wrapper -> reset line low
    ser.setRTS(True)
    time.sleep(0.05)
    ser.setDTR(False)
    ser.setRTS(False)
    time.sleep(0.15)


def wait_block_request(reader, deadline_s=3.0):
    for mtype, payload in reader.pump(deadline_s):
        if mtype == MSG_CMD_OTA_BLOCK_REQUEST:
            return payload
        elif mtype == MSG_CMD_ACKNOWLEDGE:
            print("  ack: type=0x%04x status=0x%02x" %
                  ((payload[0] << 8) | payload[1], payload[2] if len(payload) > 2 else -1))
        else:
            print("  msg 0x%04x (%d b): %s" % (mtype, len(payload), payload.hex()))
    return None


def cmd_probe(args):
    ser = open_port(args.port)
    reset_chip(ser)
    reader = Reader(ser)
    print("reset; listening 3 s for bootloader traffic...")
    req = wait_block_request(reader, 3.0)
    if req is None:
        print("no block requests. Silence is inconclusive: UART OTA may be")
        print("unavailable, use another timing, or require an unsupported command.")
        print("The probe did not send OTA START or modify staging flash.")
        return 1
    off = struct.unpack(">I", req[:4])[0]
    print("block request: offset=0x%x len=%d -> bootloader UART OTA alive" %
          (off, req[4]))
    return 0


def cmd_flash(args):
    del args
    print("flash is intentionally disabled: no validated UART write path exists.")
    print("No OTA START was sent and no staging erase was requested.")
    return 64


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("-p", "--port", default="/dev/ttyAMA0")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    f = sub.add_parser("flash")
    f.add_argument("file")
    args = p.parse_args()
    if args.cmd == "probe":
        sys.exit(cmd_probe(args))
    elif args.cmd == "flash":
        sys.exit(cmd_flash(args))


if __name__ == "__main__":
    main()
