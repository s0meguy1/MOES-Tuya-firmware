#!/usr/bin/env python3
"""Identify a 1 MiB TLSR8258 flash dump against the build-04/05/stock references.

Read-only analysis. Usage: identify.py <full_dump.bin> [refdir]
"""
import hashlib
import os
import sys


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strip_ota_header(path: str) -> bytes:
    with open(path, "rb") as f:
        raw = f.read()
    return raw[62:]


def load_refs(refdirs):
    """Return {sha256: [(path, stripped_bytes)]} for *.zigbee files."""
    refs = {}
    for d in refdirs:
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isfile(p) or not name.endswith(".zigbee"):
                continue
            stripped = strip_ota_header(p)
            refs.setdefault(sha256(stripped), []).append((p, stripped))
    return refs


def fmt_off(off):
    return "0x%06x" % off


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    dump_path = sys.argv[1]
    refdirs = sys.argv[2:] or [
        "<project-root>/work/tuyaZigbee/field-artifacts",
        "<project-root>/work/tuyaZigbee/build/light",
    ]
    with open(dump_path, "rb") as f:
        dump = f.read()

    print("dump:", dump_path, "size", len(dump))

    if len(dump) < 0x100000:
        print("WARNING: dump is smaller than 1 MiB")

    # Region digests
    regions = {
        "bootloader @0x00000 (64B)": (0x00000, 64),
        "app @0x08000 (201508B, build-04 payload)": (0x08000, 201508),
        "staging @0x70000 (64B)": (0x70000, 64),
        "NV @0xD8000 (64B)": (0xD8000, 64),
        "boardcfg @0xF8000 (64B)": (0xF8000, 64),
        "identity @0xFB000 (256B)": (0xFB000, 256),
    }
    print("\n== region digests ==")
    for label, (off, size) in regions.items():
        if off + size > len(dump):
            print(f"{label}: out of range")
            continue
        data = dump[off:off + size]
        print(f"{label}: sha256={sha256(data)}")

    # Bootloader magic
    print("\n== bootloader magic ==")
    if len(dump) >= 16:
        print("bytes @0x00:", dump[0:16].hex(" "))
        print("  TC32 reset vector:", dump[0:4].hex(" "))
        print("  magic @0x08:", dump[8:12], "expected KNLT (4b 4e 4c 54)")
    if len(dump) >= 0x8000 + 16:
        print("app bytes @0x8000:", dump[0x8000:0x8010].hex(" "))
        print("  magic @0x8008:", dump[0x8008:0x800C], "expected KNLT")

    # Compare app region against references
    print("\n== app @0x8000 vs references ==")
    refs = load_refs(refdirs)
    app = dump[0x8000:0x8000 + 201508]
    app_hash = sha256(app)
    print("dump app hash:", app_hash)
    exact = refs.get(app_hash)
    if exact:
        for p, _ in exact:
            print("EXACT MATCH:", p)
    else:
        print("no exact match; nearest by differing bytes:")
        best = []
        for rh, lst in refs.items():
            ref_bytes = lst[0][1]
            n = min(len(app), len(ref_bytes))
            diff = sum(1 for i in range(n) if app[i] != ref_bytes[i])
            diff += abs(len(app) - len(ref_bytes))
            best.append((diff, rh, lst[0][0], len(ref_bytes)))
        best.sort()
        for diff, rh, p, rlen in best[:8]:
            print(f"  diff={diff:6d}  ref_len={rlen}  {p}  {rh}")

    # Identity strings
    print("\n== identity @0xFB000 (ascii) ==")
    idb = dump[0xFB000:0xFB000 + 256]
    import re
    for m in re.finditer(rb"[ -~]{4,}", idb):
        print(f"  {fmt_off(0xFB000 + m.start())}: {m.group().decode('ascii', 'replace')}")

    # NV magic
    print("\n== NV @0xD8000 ==")
    print("  bytes:", dump[0xD8000:0xD8000 + 16].hex(" "))

    # staging bank
    print("\n== staging @0x70000 ==")
    seg = dump[0x70000:0x70000 + 16]
    print("  bytes:", seg.hex(" "), "(all 0xff => empty)")


if __name__ == "__main__":
    main()
