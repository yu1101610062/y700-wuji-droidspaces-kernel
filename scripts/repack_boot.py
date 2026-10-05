#!/usr/bin/env python3
"""Replaces the kernel in a stock boot image (header v4, kernel only).

Default (--avb keep): keep the stock, OEM-signed vbmeta blob byte-for-byte and
only move it behind the new content, rewriting the (unsigned) AVB footer --
the same thing KernelSU/Magisk do when patching init_boot/boot. The chained
vbmeta signature stays valid, so userspace libfs_avb accepts it even when the
bootloader reports androidboot.vbmeta.device_state=locked (e.g. a fake-relock
ABL), and androidboot.vbmeta.digest stays identical to stock. Only the boot
content hash no longer matches, which an actually-unlocked ABL tolerates.

--avb none: re-add a fresh hash footer with algorithm NONE. Only works when
the bootloader reports device_state=unlocked; with a fake-relock ABL first
stage init rejects it (ERROR_PUBLIC_KEY_REJECTED) and the device bootloops.

Usage:
  repack_boot.py --stock boot.img --kernel Image --out new-boot.img [--avb keep|none --avbtool avbtool.py]
"""
import argparse
import pathlib
import re
import struct
import subprocess
import sys

PAGE = 4096
FOOTER_SIZE = 64


def build_body(stock: bytes, kernel: bytes) -> bytes:
    if stock[:8] != b"ANDROID!":
        sys.exit("stock image is not an Android boot image")
    _, ramdisk_size, _, _ = struct.unpack_from("<IIII", stock, 8)
    header_version, = struct.unpack_from("<I", stock, 40)
    signature_size, = struct.unpack_from("<I", stock, 44 + 1536)
    if header_version != 4 or ramdisk_size or signature_size:
        sys.exit(f"unsupported layout: v{header_version} ramdisk={ramdisk_size} sig={signature_size}")
    if kernel[56:60] != b"ARMd":
        sys.exit("kernel is not an uncompressed arm64 Image")
    header = bytearray(stock[:PAGE])
    struct.pack_into("<I", header, 8, len(kernel))
    return bytes(header) + kernel + b"\0" * (-len(kernel) % PAGE)


def keep_vbmeta(stock: bytes, body: bytes) -> bytes:
    footer = stock[-FOOTER_SIZE:]
    if footer[:4] != b"AVBf":
        sys.exit("stock image has no AVB footer")
    major, minor, _, vbmeta_offset, vbmeta_size = struct.unpack_from(">IIQQQ", footer, 4)
    vbmeta = stock[vbmeta_offset:vbmeta_offset + vbmeta_size]
    if vbmeta[:4] != b"AVB0":
        sys.exit("stock vbmeta blob not found at footer offset")
    partition_size = len(stock)
    content_end = len(body)
    if content_end + vbmeta_size > partition_size - FOOTER_SIZE:
        sys.exit("new content + vbmeta does not fit in the partition")
    new_footer = bytearray(footer)
    struct.pack_into(">IIQQQ", new_footer, 4, major, minor, content_end, content_end, vbmeta_size)
    out = body + vbmeta
    out += b"\0" * (partition_size - FOOTER_SIZE - len(out))
    return out + bytes(new_footer)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True, type=pathlib.Path)
    ap.add_argument("--kernel", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--avb", choices=["keep", "none"], default="keep")
    ap.add_argument("--avbtool", type=pathlib.Path)
    a = ap.parse_args()

    stock = a.stock.read_bytes()
    body = build_body(stock, a.kernel.read_bytes())

    if a.avb == "keep":
        a.out.write_bytes(keep_vbmeta(stock, body))
    else:
        if not a.avbtool:
            sys.exit("--avb none needs --avbtool")
        info = subprocess.run([sys.executable, str(a.avbtool), "info_image", "--image", str(a.stock)],
                              check=True, capture_output=True, text=True).stdout
        partition_size = int(re.search(r"^Image size:\s+(\d+) bytes", info, re.M).group(1))
        rollback = int(re.search(r"^Rollback Index:\s+(\d+)", info, re.M).group(1))
        a.out.write_bytes(body)
        cmd = [sys.executable, str(a.avbtool), "add_hash_footer", "--image", str(a.out),
               "--partition_size", str(partition_size), "--partition_name", "boot",
               "--hash_algorithm", "sha256", "--algorithm", "NONE",
               "--rollback_index", str(rollback)]
        for k, v in re.findall(r"Prop: (\S+) -> '(.*)'", info):
            cmd += ["--prop", f"{k}:{v}"]
        subprocess.run(cmd, check=True)
    print(f"wrote {a.out} ({a.out.stat().st_size} bytes, avb={a.avb})")


if __name__ == "__main__":
    main()
