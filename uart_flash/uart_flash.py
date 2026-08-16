#!/usr/bin/env python3
"""TLSR825x stock-bootloader UART flash client for the Moes ZT3L bench rig.

Speakes the bootloader's own UART OTA protocol (Telink Zigbee SDK
bootloader.c): 0x55-framed messages, crc8, the bootloader ERASES its OTA
staging area, REQUESTS blocks (max 40 bytes each), CRC32-validates the
image, copies it to 0x8000 with read-back verify, and boots it. Fully
reliable flash writes - unlike one-wire SWire writes, whose UART cell
framing cannot represent bytes 0x80-0xBF on precise hosts.

Wire: Pi TXD -> 1k -> ZT3L pin 15 (RXD), Pi RXD <- ZT3L pin 16 (TXD),
RST via the rig's reset GPIO (pi_run.py handles setDTR/setRTS mapping).

Usage:
  python3 uart_flash.py -p /dev/ttyAMA0 probe
      reset the chip, listen: does the bootloader emit block requests?
      (also: nothing else talks first - silence means the boot's UART
      OTA may be disabled in this build)
  python3 uart_flash.py -p /dev/ttyAMA0 flash image.bin
      full flash: START (bootloader erases its OTA bank) then serve the
      blocks it requests until it reports END. The START response's
      flashStartAddr reveals this bootloader's real OTA staging address.
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
        end = time.time() + timeout
        while time.time() < end:
            chunk = self.ser.read(256)
            if chunk:
                self.buf += chunk
                while True:
                    msg = self._parse()
                    if msg is None:
                        break
                    yield msg
            else:
                break

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
        print("no block requests. The bootloader's UART OTA window may be")
        print("shorter than expected or disabled; try flashing anyway.")
        return 1
    off = struct.unpack(">I", req[:4])[0]
    print("block request: offset=0x%x len=%d -> bootloader UART OTA alive" %
          (off, req[4]))
    return 0


def cmd_flash(args):
    image = open(args.file, "rb").read()
    print("image: %d bytes (%s)" % (len(image), args.file))

    ser = open_port(args.port)
    reset_chip(ser)
    reader = Reader(ser)

    # START
    ser.write(frame(MSG_CMD_OTA_START_REQUEST,
                    struct.pack(">I", len(image))))
    start_rsp = None
    for mtype, payload in reader.pump(2.0):
        if mtype == MSG_CMD_OTA_START_RESPONSE:
            start_rsp = payload
            break
    if start_rsp is None or len(start_rsp) < 13:
        print("no START response - bootloader not listening on UART OTA?")
        return 1
    flash_addr, total, offset, status = struct.unpack(">IIIB", start_rsp[:13])
    print("START: status=0x%02x otaBank=0x%06x total=0x%x offset=0x%x" %
          (status, flash_addr, total, offset))
    if status != MSG_STA_SUCCESS:
        print("bootloader refused the transfer (status 0x%02x)" % status)
        return 1

    t0 = time.time()
    served = 0
    while served < len(image):
        req = wait_block_request(reader, 2.0)
        if req is None or len(req) < 5:
            print("lost the bootloader (no block request)")
            return 1
        off = struct.unpack(">I", req[:4])[0]
        blen = req[4]
        chunk = image[off:off + blen]
        if len(chunk) < blen:
            chunk = chunk + b"\xff" * (blen - len(chunk))
        ser.write(frame(MSG_CMD_OTA_BLOCK_RESPONSE,
                        bytes([MSG_STA_SUCCESS]) + struct.pack(">I", off) +
                        bytes([len(chunk)]) + chunk))
        served = off + len(chunk)
        if served % 4096 < 80 or served >= len(image):
            sys.stdout.write("\r  0x%06x / 0x%06x (%.0f B/s)   " %
                             (served, len(image), served / max(0.001, time.time() - t0)))
            sys.stdout.flush()

    print("\nall bytes served; waiting for END...")
    for mtype, payload in reader.pump(8.0):
        if mtype == MSG_CMD_OTA_END_STATUS:
            total, offset, status = struct.unpack(">IIB", payload[:9])
            print("END: status=0x%02x total=0x%x offset=0x%x" %
                  (status, total, offset))
            if status == MSG_STA_SUCCESS:
                print("bootloader validated the image, copying to 0x8000 and rebooting.")
                return 0
            return 1
        elif mtype == MSG_CMD_ACKNOWLEDGE:
            print("  ack: type=0x%04x status=0x%02x" %
                  ((payload[0] << 8) | payload[1], payload[2] if len(payload) > 2 else -1))
    print("no END status received")
    return 1


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
