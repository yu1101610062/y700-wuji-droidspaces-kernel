#!/usr/bin/env python3
"""Audits stock .ko files from a device against a newly built kernel.

For every module, every imported symbol + CRC (basic __versions and the
extended __version_ext_* sections) must be satisfied either by the new
vmlinux.symvers with an identical CRC, or by an export of another stock
module in the set (those binaries do not change). The vermagic of each module
must match the new Image after the release word, like same_magic() does
when CRCs are present. Imports that the stock kernel cannot resolve either
(modules that never load on the device) are reported as pre-existing.

Usage:
  audit_modules.py --symvers NEW/vmlinux.symvers --image NEW/Image \
      [--stock-symvers reference/gki-14494108/vmlinux.symvers] DIR [DIR ...]
"""
import argparse
import pathlib
import re
import struct
import sys


def elf_sections(data: bytes) -> dict:
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise ValueError("not ELF64")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    hdrs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    str_off, str_size = hdrs[shstrndx][4], hdrs[shstrndx][5]
    names = data[str_off:str_off + str_size]
    out = {}
    for h in hdrs:
        name = names[h[0]:names.index(b"\0", h[0])].decode()
        out[name] = data[h[4]:h[4] + h[5]] if h[1] != 8 else b""  # SHT_NOBITS
    return out


def module_info(path: pathlib.Path):
    secs = elf_sections(path.read_bytes())
    imports = {}
    vers = secs.get("__versions", b"")
    for off in range(0, len(vers) - 63, 64):
        crc, = struct.unpack_from("<Q", vers, off)
        name = vers[off + 8:off + 64].split(b"\0")[0].decode()
        imports[name] = crc & 0xFFFFFFFF
    ext_crcs = secs.get("__version_ext_crcs", b"")
    ext_names = [n.decode() for n in secs.get("__version_ext_names", b"").split(b"\0") if n]
    for i, name in enumerate(ext_names):
        crc, = struct.unpack_from("<I", ext_crcs, i * 4)
        imports.setdefault(name, crc)
    exports = {s.decode() for s in secs.get("__ksymtab_strings", b"").split(b"\0") if s}
    modinfo = dict(kv.split("=", 1) for kv in
                   (x.decode(errors="replace") for x in secs.get(".modinfo", b"").split(b"\0"))
                   if "=" in kv)
    return imports, exports, modinfo


def parse_symvers(path):
    out = {}
    for line in pathlib.Path(path).read_text().splitlines():
        f = line.split("\t")
        if len(f) >= 3:
            out[f[1]] = int(f[0], 16)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symvers", required=True, type=pathlib.Path, help="new vmlinux.symvers")
    ap.add_argument("--image", required=True, type=pathlib.Path, help="new Image")
    ap.add_argument("--stock-symvers", type=pathlib.Path, help="stock vmlinux.symvers (annotation)")
    ap.add_argument("dirs", nargs="+", type=pathlib.Path)
    a = ap.parse_args()

    new = parse_symvers(a.symvers)
    stock = parse_symvers(a.stock_symvers) if a.stock_symvers else {}
    image = a.image.read_bytes()
    m = re.search(rb"(6\.\d+\.\d+-[\w.-]+ SMP preempt mod_unload modversions aarch64)", image)
    kernel_vermagic = m.group(1).decode() if m else None

    mods = {}
    for d in a.dirs:
        for ko in sorted(d.rglob("*.ko")):
            mods[f"{d.name}/{ko.name}"] = module_info(ko)
    module_exports = set().union(*(e for _, e, _ in mods.values()))

    n_imports = n_vmlinux = n_modprov = 0
    problems, preexisting, vermagic_bad = [], [], []
    tail = lambda v: v.split(" ", 1)[1] if v and " " in v else v
    for key, (imports, _, modinfo) in mods.items():
        if tail(modinfo.get("vermagic")) != tail(kernel_vermagic):
            vermagic_bad.append((key, modinfo.get("vermagic")))
        for sym, crc in imports.items():
            n_imports += 1
            if sym in new:
                n_vmlinux += 1
                if new[sym] != crc:
                    problems.append(f"CRC  {key}: {sym} module=0x{crc:08x} new=0x{new[sym]:08x} "
                                    f"stock={hex(stock[sym]) if sym in stock else '-'}")
            elif sym in module_exports:
                n_modprov += 1
            elif stock and sym not in stock:
                preexisting.append(f"{key}: {sym}")
            else:
                problems.append(f"MISS {key}: {sym} 0x{crc:08x}")

    print(f"kernel vermagic : {kernel_vermagic}")
    print(f"modules         : {len(mods)}")
    print(f"imports         : {n_imports} (vmlinux {n_vmlinux}, module-provided {n_modprov})")
    print(f"CRC/missing     : {len(problems)}")
    print(f"vermagic differ : {len(vermagic_bad)} (compared after the release word)")
    print(f"pre-existing    : {len(preexisting)} import(s) unresolved on stock too: {preexisting}")
    for p in problems[:200]:
        print("  " + p)
    for k, v in vermagic_bad[:20]:
        print(f"  VERMAGIC {k}: {v}")
    ok = not problems and not vermagic_bad
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
