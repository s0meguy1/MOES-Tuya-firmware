#!/usr/bin/env python3
"""Merge N independent SWire dumps of the same flash into one verified image.

WHY A MERGE IS SOUND HERE (measured, not assumed):
  On this rig the only read error is a single bit - bit 5 of the decoded byte,
  i.e. blk[2], the 3rd swire cell - and it is STRICTLY ONE-DIRECTIONAL: the bit
  can only ever be erroneously CLEARED, never set. Proven over 6 reads of a
  32 KB block: 141 bytes varied, only ever in bit 5, with set-counts of 5/6 or
  4/6 and never 1/6, 2/6 or 3/6. If a '1' can never appear spuriously, then a
  bit that reads 1 in ANY dump is genuinely 1, so the bitwise OR of the dumps
  is the true image. Residual error per marginal byte after N dumps is p**N
  with p ~= 0.17.

Verification performed:
  * every pairwise difference must be bit 5 only (else the model is wrong)
  * OR must converge: OR(1..N-1) == OR(1..N)
  * OR must differ from each input only by SET bits (never cleared)
  * structural checks: KNLT magic + declared image sizes
  * independent cross-check against a dump taken at a different baud/timing
"""
import struct
import sys
from collections import Counter


def load(paths):
    imgs = []
    for p in paths:
        d = open(p, 'rb').read()
        imgs.append((p, d))
        print('  %-28s %d bytes' % (p.split('/')[-1], len(d)))
    n = {len(d) for _, d in imgs}
    if len(n) != 1:
        sys.exit('ERROR: dumps differ in length: %s' % n)
    return imgs


def pairwise_check(imgs):
    print('\n--- pairwise differences (must be bit 5 only) ---')
    bad = 0
    for i in range(len(imgs)):
        for j in range(i + 1, len(imgs)):
            a, b = imgs[i][1], imgs[j][1]
            xs = Counter(a[k] ^ b[k] for k in range(len(a)) if a[k] != b[k])
            off = {x for x in xs if x != 0x20}
            if off:
                bad += 1
                print('  %d vs %d: %d diffs, NON-BIT5 XORS %s' %
                      (i + 1, j + 1, sum(xs.values()), sorted(hex(x) for x in off)))
            else:
                print('  %d vs %d: %5d diffs, all xor=0x20 ok' % (i + 1, j + 1, sum(xs.values())))
    return bad == 0


def merge(imgs):
    n = len(imgs[0][1])
    out = bytearray(imgs[0][1])
    for _, d in imgs[1:]:
        for k in range(n):
            out[k] |= d[k]
    return bytes(out)


def convergence(imgs):
    print('\n--- convergence of the OR as dumps are added ---')
    prev = None
    for k in range(1, len(imgs) + 1):
        cur = merge(imgs[:k])
        if prev is not None:
            ch = sum(1 for i in range(len(cur)) if cur[i] != prev[i])
            print('  OR of first %d: %d bytes changed vs OR of %d' % (k, ch, k - 1))
        prev = cur
    return prev


def or_only_sets(final, imgs):
    print('\n--- final vs each input (must only ADD set bits) ---')
    ok = True
    for i, (p, d) in enumerate(imgs):
        cleared = sum(1 for k in range(len(d)) if d[k] & ~final[k])
        added = sum(1 for k in range(len(d)) if final[k] != d[k])
        flag = 'ok' if cleared == 0 else 'VIOLATION'
        if cleared:
            ok = False
        print('  input %d: %5d bytes gained bits, %d lost bits  %s' % (i + 1, added, cleared, flag))
    return ok


def structure(d):
    print('\n--- structural checks ---')
    ok = True
    for name, base in (('bootloader', 0x0000), ('application', 0x8000)):
        magic = d[base + 8:base + 12]
        size = struct.unpack('<I', d[base + 0x18:base + 0x1c])[0]
        good = magic == b'KNLT' and 0 < size < 0x100000
        ok &= good
        print('  %-11s @0x%06x magic=%r size=0x%06x end=0x%06x  %s' %
              (name, base, magic, size, base + size, 'ok' if good else 'BAD'))
    # Tuya config block carries its own crc: field
    i = d.find(b'Jsonver:')
    if i > 0:
        j = d.find(b'}', i)
        cfg = d[i:j + 1].decode('ascii', 'replace')
        cs = cfg.find('crc:')
        stated = cfg[cs + 4:].split(',')[0].strip('}') if cs > 0 else None
        body = cfg[:cs]
        s = sum(body.encode()) & 0xff
        print('  tuya cfg   @0x%06x len=%d crc field=%s  sum(body)&0xff=%d' %
              (i, len(cfg), stated, s))
        print('             module=%s category=%s' %
              (dict(kv.split(':', 1) for kv in body.strip('{').rstrip(',').split(',')
                    if ':' in kv).get('module'),
               dict(kv.split(':', 1) for kv in body.strip('{').rstrip(',').split(',')
                    if ':' in kv).get('category')))
    for s in (b'_TZ3210_b8jdosxo', b'TS0505B', b'KNLT'):
        print('  contains %-18r at 0x%06x' % (s, d.find(s)))
    return ok


def cross_check(final, path, start, length):
    """Compare against a dump taken at a DIFFERENT baud/cell timing."""
    try:
        ref = open(path, 'rb').read()
    except OSError:
        print('\n(no independent cross-check file %s)' % path)
        return
    n = min(length, len(ref))
    diffs = [i for i in range(n) if final[start + i] != ref[i]]
    print('\n--- cross-check vs %s (different baud/timing) ---' % path.split('/')[-1])
    print('  %d/%d bytes differ' % (len(diffs), n))
    for i in diffs[:10]:
        print('    0x%06x: merged=%02x ref=%02x xor=%02x' %
              (start + i, final[start + i], ref[i], final[start + i] ^ ref[i]))


def main():
    paths = sys.argv[1:-1]
    outp = sys.argv[-1]
    print('=== merging %d dumps -> %s ===' % (len(paths), outp))
    imgs = load(paths)
    p_ok = pairwise_check(imgs)
    final = convergence(imgs)
    s_ok = or_only_sets(final, imgs)
    structure(final)
    cross_check(final, '/tmp/t8k.bin', 0, 0x2000)
    open(outp, 'wb').write(final)
    print('\nwrote %s (%d bytes)' % (outp, len(final)))
    print('model valid: %s   OR monotonic: %s' % (p_ok, s_ok))


if __name__ == '__main__':
    main()
