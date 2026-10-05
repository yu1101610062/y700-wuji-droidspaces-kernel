#!/usr/bin/env python3
"""Verifies a Droidspaces GKI build against the stock GKI build it replaces.

Checks (any failure exits non-zero):
  1. .config differs from stock only in the Droidspaces options.
  2. Every stock exported symbol (vmlinux + GKI modules) still exists with the
     same CRC, owner and export type. Only additions are allowed.
  3. Kernel release string is identical to stock.
  4. The stock GKI module signing certificate is embedded (stock system_dlkm
     modules keep sig_ok).
  5. BTF layout of every named struct/union/enum is identical to stock, except
     the expected SYSVIPC members placed in the task_struct alignment hole.
"""
import argparse
import base64
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import btf  # noqa: E402

EXPECTED_RELEASE = "6.12.38-android16-5-g74ad46052215-ab14494108-4k"

# Options this build is supposed to change, with the value expected in the
# final .config (None means "is not set").
EXPECTED_CONFIG = {
    "CONFIG_SYSVIPC": "y",
    "CONFIG_SYSVIPC_SYSCTL": "y",
    "CONFIG_SYSVIPC_COMPAT": "y",
    "CONFIG_POSIX_MQUEUE": "y",
    "CONFIG_POSIX_MQUEUE_SYSCTL": "y",
    "CONFIG_IPC_NS": "y",
    "CONFIG_PID_NS": "y",
    "CONFIG_USER_NS": "y",
    "CONFIG_DEVTMPFS": "y",
    "CONFIG_DEVTMPFS_MOUNT": None,
    "CONFIG_DEVTMPFS_SAFE": None,
    "CONFIG_SYSTEM_TRUSTED_KEYS": '"certs/stock_gki_signing_cert.pem"',
}
# Options that must keep their stock value; checked explicitly because the
# packed SYSVIPC fields rely on plain (non-LDAR) READ_ONCE, i.e. no LTO.
PINNED_CONFIG = {"CONFIG_LTO_NONE": "y", "CONFIG_GENDWARFKSYMS": "y", "CONFIG_MODVERSIONS": "y"}

# init_ipc_ns/put_ipc_ns: patch 0002. The uid/gid mappers become real (no
# longer inline) functions with USER_NS=y and in-tree GKI modules import them.
EXPECTED_NEW_EXPORTS = {"init_ipc_ns", "put_ipc_ns", "from_kgid", "from_kgid_munged",
                        "from_kuid", "from_kuid_munged", "make_kgid", "make_kuid"}

# task_struct: two anonymous packed unions in the 164..192 hole.
EXPECTED_TASK_STRUCT_ADDED = [(164 * 8, 8), (172 * 8, 16)]


def parse_config(path):
    cfg = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if m := re.match(r"^(CONFIG_\w+)=(.*)$", line):
            cfg[m.group(1)] = m.group(2)
        elif m := re.match(r"^# (CONFIG_\w+) is not set$", line):
            cfg[m.group(1)] = None
    return cfg


def parse_symvers(path):
    out = {}
    for line in pathlib.Path(path).read_text().splitlines():
        f = line.split("\t")
        if len(f) >= 4:
            out[f[1]] = (f[0].lower(), f[2], f[3], f[4] if len(f) > 4 else "")
    return out


class Report:
    def __init__(self):
        self.lines, self.failed = [], False

    def ok(self, msg):
        self.lines.append(f"- PASS {msg}")

    def fail(self, msg):
        self.failed = True
        self.lines.append(f"- **FAIL** {msg}")

    def info(self, msg):
        self.lines.append(f"  - {msg}")


def check_config(rep, stock_cfg, new_cfg):
    keys = set(stock_cfg) | set(new_cfg)
    diffs = {k for k in keys if stock_cfg.get(k, "<absent>") != new_cfg.get(k, "<absent>")}
    unexpected = sorted(d for d in diffs if d not in EXPECTED_CONFIG)
    wrong = sorted(k for k, v in EXPECTED_CONFIG.items() if new_cfg.get(k, None) != v)
    pinned = sorted(k for k, v in PINNED_CONFIG.items() if new_cfg.get(k) != v)
    if unexpected or wrong or pinned:
        rep.fail(f".config: unexpected={unexpected} wrong_value={wrong} pinned_changed={pinned}")
        for k in unexpected + wrong + pinned:
            rep.info(f"{k}: stock={stock_cfg.get(k, '<absent>')} new={new_cfg.get(k, '<absent>')}")
    else:
        rep.ok(f".config: {len(diffs)} option(s) changed, all expected")
    for k in sorted(diffs):
        rep.info(f"{k}: {stock_cfg.get(k, '<absent>')} -> {new_cfg.get(k, '<absent>')}")


def check_symvers(rep, label, stock, new):
    removed = sorted(s for s in stock if s not in new)
    changed = sorted(s for s in stock if s in new and new[s] != stock[s])
    added = sorted(s for s in new if s not in stock)
    if removed or changed:
        rep.fail(f"{label}: removed={len(removed)} changed={len(changed)} added={len(added)}")
        for s in (removed + changed)[:200]:
            rep.info(f"{s}: stock={stock.get(s)} new={new.get(s)}")
    else:
        rep.ok(f"{label}: {len(stock)} stock exports unchanged (CRC/owner/type), added={added}")
    if label.startswith("Module") and set(added) - EXPECTED_NEW_EXPORTS:
        rep.info(f"note: additional new exports {sorted(set(added) - EXPECTED_NEW_EXPORTS)}")


def check_btf(rep, stock_btf, new_btf):
    s_aggr, n_aggr = stock_btf.named_aggregates(), new_btf.named_aggregates()
    s_enum, n_enum = stock_btf.named_enums(), new_btf.named_enums()
    changed = sorted(k for k in s_aggr if k in n_aggr and s_aggr[k] != n_aggr[k])
    changed_enums = sorted(k for k in s_enum if k in n_enum and s_enum[k] != n_enum[k])
    removed = sorted(k for k in s_aggr if k not in n_aggr)
    added = sorted(k for k in n_aggr if k not in s_aggr)

    allowed = {"struct task_struct", "struct sysv_sem", "struct sysv_shm"}
    bad = [k for k in changed if k not in allowed] + changed_enums
    if bad:
        rep.fail(f"BTF: unexpected layout changes in {bad}")
        for k in bad[:50]:
            rep.info(f"{k}\n    stock={sorted(s_aggr.get(k, s_enum.get(k)))}\n    new  ={sorted(n_aggr.get(k, n_enum.get(k)))}")
    else:
        rep.ok(f"BTF: {len(s_aggr)} named aggregates + {len(s_enum)} enums compared; "
               f"changed only {changed}")
    rep.info(f"types only in new build ({len(added)}): {added[:60]}")
    rep.info(f"types only in stock ({len(removed)}): {removed[:60]}")

    # task_struct must be stock + exactly the two hole members.
    s_ts = list(stock_btf.members("task_struct"))
    n_ts = list(new_btf.members("task_struct"))
    s_set = {(n, o, sz, t) for n, o, sz, t in s_ts}
    extra = [m for m in n_ts if m not in s_set]
    missing = [m for m in s_ts if m not in set(n_ts)]
    got = sorted((o * 8, sz) for n, o, sz, t in extra)
    size_s = next(t["size"] for t in stock_btf.types[1:] if t["kind"] == btf.KIND_STRUCT and t["name"] == "task_struct")
    size_n = next(t["size"] for t in new_btf.types[1:] if t["kind"] == btf.KIND_STRUCT and t["name"] == "task_struct")
    if missing or got != EXPECTED_TASK_STRUCT_ADDED or size_s != size_n:
        rep.fail(f"task_struct: size {size_s}->{size_n}, missing={missing}, extra={extra}")
    else:
        rep.ok(f"task_struct: size {size_n} unchanged; every stock member at the same offset; "
               f"added {[(o, sz, t) for n, o, sz, t in extra]}")
    se = [m for m in n_ts if m[0] == "se"]
    rep.info(f"task_struct.se = {se}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, type=pathlib.Path)
    ap.add_argument("--ref", required=True, type=pathlib.Path)
    ap.add_argument("--stock-image", required=True, type=pathlib.Path)
    ap.add_argument("--cert", required=True, type=pathlib.Path)
    ap.add_argument("--report", type=pathlib.Path)
    a = ap.parse_args()

    rep = Report()
    image = (a.dist / "Image").read_bytes()

    check_config(rep, parse_config(a.ref / "kernel_aarch64_dot_config"),
                 parse_config(a.dist / "kernel_aarch64_dot_config"))
    check_symvers(rep, "vmlinux.symvers", parse_symvers(a.ref / "vmlinux.symvers"),
                  parse_symvers(a.dist / "vmlinux.symvers"))
    check_symvers(rep, "Module.symvers", parse_symvers(a.ref / "kernel_aarch64_Module.symvers"),
                  parse_symvers(a.dist / "kernel_aarch64_Module.symvers"))

    banner = re.search(rb"Linux version (\S+)", image)
    release = banner.group(1).decode() if banner else None
    if release == EXPECTED_RELEASE:
        rep.ok(f"release: {release}")
    else:
        rep.fail(f"release: got {release}, want {EXPECTED_RELEASE}")

    pem = a.cert.read_text()
    der = base64.b64decode("".join(l for l in pem.splitlines() if not l.startswith("-----")))
    if der in image:
        rep.ok(f"stock GKI signing cert embedded ({len(der)} bytes DER)")
    else:
        rep.fail("stock GKI signing cert NOT found in Image")

    check_btf(rep, btf.BTF(btf.extract_from_image(a.stock_image.read_bytes())),
              btf.BTF(btf.extract_from_image(image)))

    text = "\n".join(["# Build verification", "", *rep.lines, "",
                      "RESULT: " + ("FAIL" if rep.failed else "PASS"), ""])
    print(text)
    if a.report:
        a.report.write_text(text)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
