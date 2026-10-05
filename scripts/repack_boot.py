#!/usr/bin/env python3
"""Replaces the kernel in a stock boot image (header v4, kernel only).

Keeps the stock boot header fields, drops the stale GKI certification blob
that followed the old kernel, and re-adds an AVB hash footer with the stock
partition size, rollback index and com.android.build.boot.* properties
(algorithm NONE; only meaningful on an unlocked bootloader).

Usage:
  repack_boot.py --stock boot.img --kernel Image --avbtool avbtool.py --out new-boot.img
"""
import argparse
import pathlib
import re
import struct
import subprocess
import sys

PAGE = 4096


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True, type=pathlib.Path)
    ap.add_argument("--kernel", required=True, type=pathlib.Path)
    ap.add_argument("--avbtool", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    a = ap.parse_args()

    stock = a.stock.read_bytes()
    kernel = a.kernel.read_bytes()
    if stock[:8] != b"ANDROID!":
        sys.exit("stock image is not an Android boot image")
    _, ramdisk_size, _, _ = struct.unpack_from("<IIII", stock, 8)
    header_version, = struct.unpack_from("<I", stock, 40)
    signature_size, = struct.unpack_from("<I", stock, 44 + 1536)
    if header_version != 4 or ramdisk_size or signature_size:
        sys.exit(f"unsupported layout: v{header_version} ramdisk={ramdisk_size} sig={signature_size}")
    if kernel[56:60] != b"ARMd":
        sys.exit("kernel is not an uncompressed arm64 Image")

    info = subprocess.run([sys.executable, str(a.avbtool), "info_image", "--image", str(a.stock)],
                          check=True, capture_output=True, text=True).stdout
    partition_size = int(re.search(r"^Image size:\s+(\d+) bytes", info, re.M).group(1))
    rollback = int(re.search(r"^Rollback Index:\s+(\d+)", info, re.M).group(1))
    props = re.findall(r"Prop: (\S+) -> '(.*)'", info)

    header = bytearray(stock[:PAGE])
    struct.pack_into("<I", header, 8, len(kernel))
    body = bytes(header) + kernel + b"\0" * (-len(kernel) % PAGE)
    a.out.write_bytes(body)

    cmd = [sys.executable, str(a.avbtool), "add_hash_footer", "--image", str(a.out),
           "--partition_size", str(partition_size), "--partition_name", "boot",
           "--hash_algorithm", "sha256", "--algorithm", "NONE",
           "--rollback_index", str(rollback)]
    for k, v in props:
        cmd += ["--prop", f"{k}:{v}"]
    subprocess.run(cmd, check=True)
    print(subprocess.run([sys.executable, str(a.avbtool), "info_image", "--image", str(a.out)],
                         check=True, capture_output=True, text=True).stdout)
    print(f"wrote {a.out} ({a.out.stat().st_size} bytes, kernel {len(kernel)} bytes)")


if __name__ == "__main__":
    main()
