#!/usr/bin/env python3
"""swire_diag.py - instrumented diagnostics for the ZT3L / TLSR8258 SWire dump stall.

Run under pi_run.py so setDTR() lands on the reset GPIO:

    python3 pi_run.py swire_diag.py env
    python3 pi_run.py swire_diag.py autopsy [start_offset] [rdsize]
    python3 pi_run.py swire_diag.py window  [rdsize] [reps]
    python3 pi_run.py swire_diag.py life    [seconds]

Everything here reuses TLSR825xComFlasher primitives so we are measuring the
real code path, not a re-implementation.
"""
import contextlib
import io
import os
import subprocess
import sys
import time

import serial

import TLSR825xComFlasher as F

PORT = os.environ.get('SWS_PORT', '/dev/ttyAMA0')
BAUD = int(os.environ.get('SWS_BAUD', '921600'))
REF = os.environ.get('SWS_REF', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'seg_00.bin'))


# ---------------------------------------------------------------- plumbing
@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def tty_counters():
    """fe/brk/oe counters for ttyAMA0 straight from the kernel."""
    try:
        out = subprocess.run(['sudo', 'cat', '/proc/tty/driver/ttyAMA'],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception as e:
        return {'err': str(e)}
    for line in out.splitlines():
        if line.startswith('0:'):
            d = {}
            for tok in line.split():
                if ':' in tok:
                    k, _, v = tok.partition(':')
                    if v.isdigit():
                        d[k] = int(v)
            return d
    return {}


def cdelta(a, b):
    return {k: b.get(k, 0) - a.get(k, 0) for k in set(a) | set(b)
            if b.get(k, 0) - a.get(k, 0)}


def open_port():
    p = serial.Serial(PORT, BAUD)
    p.setDTR(False)            # TRAP: pyserial asserts DTR (= our RESET) on open
    p.reset_input_buffer()
    p.timeout = 0.1
    return p


def auto_div(p):
    """set_sws_auto_speed(), but quiet and returns the swsdiv it settled on."""
    div = int(round(16000000 * 2 / p.baudrate))
    divmax = int(round(48000000 * 2 / p.baudrate))
    bit8m = ((~(F.bit8mask - 1)) << 1) & 0xff
    while div <= divmax:
        F.rd_sws_wr_addr_usbcom(p, 0x00b2, bytearray([div]))
        F.rd_wr_usbcom_blk(p, F.sws_rd_addr(0x00b2))
        p.write([0xfe])
        blk = p.read(9)
        if len(blk) < 9:
            blk += p.read(9 - len(blk))
        F.rd_wr_usbcom_blk(p, F.sws_code_end())
        if len(blk) == 9 and blk[8] == 0xfe:
            c = F.sws_encode_blk([div])
            if ((blk[0] & bit8m) == bit8m and blk[1] == c[2] and blk[2] == c[3]
                    and blk[4] == c[5] and blk[6] == c[7] and blk[7] == c[8]):
                return div
        div += 1
    return None


def sync(p, tact=200, tries=80):
    for i in range(tries):
        with quiet():
            F.activate(p, tact)
            div = auto_div(p)
        if div:
            return i + 1, div
    return 0, None


def set_div(p, div):
    F.rd_sws_wr_addr_usbcom(p, 0x00b2, bytearray([div]))


# ------------------------------------------------------- instrumented I/O
def read_data(p, addr, size=1, settle=0.05):
    """sws_read_data() with the failure captured instead of thrown away."""
    time.sleep(settle)
    p.reset_input_buffer()
    F.rd_wr_usbcom_blk(p, F.sws_rd_addr(addr))
    out = []
    err = None
    for i in range(size):
        p.write([0xfe])
        blk = p.read(9)
        short = len(blk)
        if len(blk) < 9:
            blk += p.read(10 - len(blk))
        x = F.sws_decode_blk(blk)
        if x is None:
            err = {'idx': i, 'blk': bytes(blk), 'first_read_len': short,
                   'waiting': p.in_waiting}
            F.rd_wr_usbcom_blk(p, F.sws_code_end())
            out = None
            break
        out.append(x)
    F.rd_wr_usbcom_blk(p, F.sws_code_end())
    return out, err


def flash_read(p, offset, n, settle=0.05):
    F.rd_sws_wr_addr_usbcom(p, 0x0b3, bytearray([0x80]))
    F.rd_sws_wr_addr_usbcom(p, 0x0d, bytearray([0x00]))
    F.rd_sws_wr_addr_usbcom(p, 0x0c, bytearray(
        [0x03, (offset >> 16) & 0xff, (offset >> 8) & 0xff, offset & 0xff, 0]))
    F.rd_sws_wr_addr_usbcom(p, 0x0d, bytearray([0x0A]))
    data, err = read_data(p, 0x0c, n, settle)
    F.rd_sws_wr_addr_usbcom(p, 0x0d, bytearray([0x01]))
    F.rd_sws_wr_addr_usbcom(p, 0x0b3, bytearray([0x00]))
    return data, err


def regs(p, addr, n):
    d, e = read_data(p, addr, n, settle=0.02)
    return d


def show_regs(p, tag):
    tmr = regs(p, 0x620, 4)
    cpu = regs(p, 0x0602, 1)
    b2 = regs(p, 0x00b2, 1)
    print('  %-14s tmr_ctrl[620..623]=%s  cpu[602]=%s  swsdiv[b2]=%s' % (
        tag,
        ' '.join('%02x' % x for x in tmr) if tmr else 'FAIL',
        '%02x' % cpu[0] if cpu else 'FAIL',
        '%02x' % b2[0] if b2 else 'FAIL'))
    return tmr


def load_ref():
    try:
        with open(REF, 'rb') as f:
            return f.read()
    except Exception:
        return b''


# ------------------------------------------------------------ subcommands
def cmd_env(p):
    print('port %s @ %d, pyserial %s' % (PORT, BAUD, serial.__version__))
    print('tty counters:', tty_counters())
    n, div = sync(p)
    print('synced on attempt %s, swsdiv=%s (SW-CLK ~%.2f MHz)' %
          (n, div, BAUD * div / 2e6 if div else 0))
    show_regs(p, 'after sync:')
    print('  writing [0x622]=0x00 ...')
    F.rd_sws_wr_addr_usbcom(p, 0x622, bytearray([0x00]))
    show_regs(p, 'after WD wr:')
    print('  writing [0x623]=0x00 (FLD_TMR_WD_EN=BIT23 lives at 622 bit7;')
    print('   623 is reg_tmr_sta - clearing it is the wd_clear path) ...')
    F.rd_sws_wr_addr_usbcom(p, 0x620, bytearray([0x00, 0x00, 0x00, 0x00]))
    show_regs(p, 'after 620=0:')


def cmd_autopsy(p, start=0x0000, rdsize=0x100):
    ref = load_ref()
    c0 = tty_counters()
    t0 = time.time()
    n, div = sync(p)
    tsync = time.time()
    print('synced attempt=%s swsdiv=%s in %.1fs' % (n, div, tsync - t0))
    show_regs(p, 'post-sync:')

    off = start
    chunks = 0
    bad = 0
    print('--- streaming until first failure (rdsize=0x%x) ---' % rdsize)
    while time.time() - tsync < 600:
        data, err = flash_read(p, off, rdsize)
        if err is not None or data is None:
            break
        if ref and off + rdsize <= len(ref):
            got = bytes(data)
            exp = ref[off:off + rdsize]
            if got != exp:
                bad += 1
                print('  MISMATCH at 0x%06x (silent corruption!)' % off)
        chunks += 1
        off += rdsize
    dt = time.time() - tsync
    print('FIRST FAILURE at 0x%06x after %d chunks / %d bytes / %.1f s '
          '(%d silent mismatches)' % (off, chunks, chunks * rdsize, dt, bad))
    print('  byte idx in chunk: %d  (offset 0x%06x)' % (err['idx'], off + err['idx']))
    print('  first read() returned %d of 9 bytes; in_waiting after=%d' %
          (err['first_read_len'], err['waiting']))
    print('  blk = %s' % ' '.join('%02x' % b for b in err['blk']))
    print('  tty delta:', cdelta(c0, tty_counters()))

    print('--- 12 bare retries of the SAME chunk, no re-activate ---')
    ok = 0
    for i in range(12):
        data, err2 = flash_read(p, off, rdsize)
        if data is not None:
            ok += 1
        else:
            print('    retry %2d: fail at byte %d, blk=%s, rd1=%d' % (
                i, err2['idx'], ' '.join('%02x' % b for b in err2['blk']),
                err2['first_read_len']))
    print('  bare retries OK: %d/12' % ok)
    show_regs(p, 'after fails:')

    print('--- single-byte register reads still working? ---')
    good = sum(1 for _ in range(20) if regs(p, 0x0602, 1))
    print('  1-byte reads of [0x602] OK: %d/20' % good)

    print('--- shrink rdsize on the SAME chunk ---')
    for rs in (0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x01):
        okn = 0
        for _ in range(6):
            d, _e = flash_read(p, off, rs)
            if d is not None:
                okn += 1
        print('  rdsize 0x%02x: %d/6 ok' % (rs, okn))

    print('--- re-activate + re-sync, then retry ---')
    n2, div2 = sync(p)
    print('  re-synced attempt=%s swsdiv=%s' % (n2, div2))
    show_regs(p, 'post-resync:')
    ok = 0
    for i in range(12):
        data, err3 = flash_read(p, off, rdsize)
        if data is not None:
            ok += 1
        elif i < 3:
            print('    retry %2d: fail at byte %d, blk=%s' % (
                i, err3['idx'], ' '.join('%02x' % b for b in err3['blk'])))
    print('  post-resync retries OK: %d/12' % ok)
    print('  tty delta total:', cdelta(c0, tty_counters()))


def find_erased(p):
    """Locate a 0xFF-filled (erased) flash region - the worst case for SWire."""
    for off in (0x0F0000, 0x0C0000, 0x080000, 0x040000, 0x008000):
        hits = 0
        for _ in range(4):
            d, _e = flash_read(p, off, 8, settle=0.02)
            if d is not None and all(b == 0xff for b in d):
                hits += 1
        if hits >= 3:
            return off
    return None


def cmd_window(p, rdsize=0x100, reps=4):
    """Map the swsdiv tolerance window.

    Two metrics, neither of which echo can fake:
      A. 0x40 bytes at 0x100 compared byte-for-byte to seg_00.bin (mixed data,
         contains 8 x 0xff).
      B. rdsize bytes from an ERASED region - a solid run of 0xff, i.e. the
         worst case for framing and the thing a full dump actually needs.
    """
    ref = load_ref()
    if len(ref) < 0x2000:
        print('need seg_00.bin reference (have %d bytes)' % len(ref))
        return
    n, base = sync(p)
    print('synced attempt=%s auto swsdiv=%s (the sweep returns the LOWEST '
          'div that decodes ONE byte)' % (n, base))
    erased = find_erased(p)
    print('erased (all-0xff) probe region: %s' %
          ('0x%06x' % erased if erased else 'NOT FOUND'))
    print()
    print(' div   MHz    A:exact/%d  B:ff-run 0x%x  verdict' % (reps, rdsize))
    results = {}
    for d in range(max(20, base - 3), min(0x7f, base + 20) + 1):
        set_div(p, d)
        exact = 0
        for r in range(reps):
            off = 0x100 + r * 0x40
            data, err = flash_read(p, off, 0x40)
            if data is not None and bytes(data) == ref[off:off + 0x40]:
                exact += 1
        ffok = 0
        if erased is not None:
            for _ in range(reps):
                data, err = flash_read(p, erased, rdsize)
                if data is not None and all(b == 0xff for b in data):
                    ffok += 1
        results[d] = (exact, ffok)
        verdict = 'PASS' if (exact == reps and ffok == reps) else (
            'partial' if (exact or ffok) else 'fail')
        print(' %3d  %6.2f   %d/%d         %d/%d          %s' %
              (d, BAUD * d / 2e6, exact, reps, ffok, reps, verdict))
        if exact == 0 and ffok == 0:
            set_div(p, base)
            if regs(p, 0x0602, 1) is None:
                n2, base2 = sync(p)
                if base2:
                    base = base2
                    print('   (lost the chip, re-synced; div now %d)' % base)
                    erased = erased or find_erased(p)
    good = [d for d, (e, f) in results.items() if e == reps and f == reps]
    if good:
        print('\nFULLY GOOD DIVS: %s' % good)
        print('CENTRE OF WINDOW -> use swsdiv %d' % ((min(good) + max(good)) // 2))
    else:
        print('\nno fully-good div; partial: %s' %
              [d for d, (e, f) in results.items() if e or f])
    set_div(p, base)


def cmd_bytetest(p, reps=200):
    """Per-byte-VALUE failure rate. Isolates value from sequence."""
    ref = load_ref()
    n, base = sync(p)
    print('synced attempt=%s swsdiv=%s' % (n, base))
    erased = find_erased(p)
    print('erased region: %s\n' % ('0x%06x' % erased if erased else 'none'))

    # every distinct value present in 0x100..0x1ff, one representative offset
    seen = {}
    for o in range(0x100, 0x200):
        seen.setdefault(ref[o], o)
    cases = [('0x%02x @0x%03x' % (v, o), o, v) for v, o in sorted(seen.items())]
    if erased:
        cases.append(('0xff @erased', erased, 0xff))

    print('%-16s %6s %8s %8s' % ('case', 'fails', 'rate', 'wrong'))
    for name, off, val in cases:
        fails = wrong = 0
        for _ in range(reps):
            d, e = flash_read(p, off, 1, settle=0.0)
            if d is None:
                fails += 1
            elif d[0] != val:
                wrong += 1
        print('%-16s %4d/%d %7.1f%% %8d' % (name, fails, reps,
                                            100.0 * fails / reps, wrong))

    print('\n--- consecutive-0xff run length from erased region ---')
    if erased:
        for nrun in (1, 2, 4, 8, 16, 32, 64, 128, 256):
            ok = 0
            t0 = time.time()
            for _ in range(8):
                d, e = flash_read(p, erased, nrun, settle=0.0)
                if d is not None and all(b == 0xff for b in d):
                    ok += 1
            print('  run 0x%03x: %d/8 ok   (%.1f ms/chunk)' %
                  (nrun, ok, (time.time() - t0) * 1000 / 8))

    print('\n--- same, from MIXED data at 0x100 (has 8 x 0xff) ---')
    for nrun in (0x10, 0x20, 0x40, 0x80, 0x100):
        ok = 0
        for _ in range(8):
            d, e = flash_read(p, 0x100, nrun, settle=0.0)
            if d is not None and bytes(d) == ref[0x100:0x100 + nrun]:
                ok += 1
        nff = ref[0x100:0x100 + nrun].count(0xff)
        print('  run 0x%03x (%d x ff): %d/8 ok' % (nrun, nff, ok))


def cmd_life(p, seconds=120):
    """Is the death time-based? Poll one register, do nothing else."""
    c0 = tty_counters()
    n, div = sync(p)
    t0 = time.time()
    print('synced attempt=%s swsdiv=%s; polling [0x602] every 100 ms' % (n, div))
    i = 0
    fails = 0
    while time.time() - t0 < seconds:
        d = regs(p, 0x0602, 1)
        i += 1
        if d is None:
            fails += 1
            print('  FAIL #%d at t=%.1fs poll=%d' % (fails, time.time() - t0, i))
            if fails >= 5:
                break
        time.sleep(0.1)
    print('polls=%d fails=%d over %.1fs' % (i, fails, time.time() - t0))
    show_regs(p, 'end:')
    print('  tty delta:', cdelta(c0, tty_counters()))


def cmd_bitcheck(p, reps=25):
    """Are the unstable bit-5 bytes random per read, or fixed per address?"""
    from collections import Counter
    addrs = [int(x, 0) for x in open('/tmp/diffaddr.txt').read().split()]
    n, base = sync(p)
    d = os.environ.get('SWS_DIV')
    if d:
        set_div(p, int(d, 0))
    print('synced attempt=%s auto div=%s forced=%s reps=%d' % (n, base, d, reps))
    unstable = 0
    for off in addrs:
        c = Counter()
        for _ in range(reps):
            v, e = flash_read(p, off, 1, settle=0.0)
            c[v[0] if v else None] += 1
        vals = sorted(c.items(), key=lambda kv: -kv[1])
        tag = 'STABLE' if len(c) == 1 else 'FLIPS '
        if len(c) > 1:
            unstable += 1
        print('  0x%06x %s %s' % (off, tag,
              ' '.join('%02x:%d' % (k, v) if k is not None else 'ERR:%d' % v
                       for k, v in vals)))
    print('%d/%d addresses flip within one run' % (unstable, len(addrs)))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'env'
    args = [int(a, 0) for a in sys.argv[2:]]
    p = open_port()
    try:
        if cmd == 'env':
            cmd_env(p)
        elif cmd == 'autopsy':
            cmd_autopsy(p, *args)
        elif cmd == 'window':
            cmd_window(p, *args)
        elif cmd == 'bytetest':
            cmd_bytetest(p, *args)
        elif cmd == 'bitcheck':
            cmd_bitcheck(p, *args)
        elif cmd == 'life':
            cmd_life(p, *args)
        else:
            print(__doc__)
    finally:
        p.close()


if __name__ == '__main__':
    main()
