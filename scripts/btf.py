"""Minimal BTF reader used to compare struct layouts between two kernels.

The kernel embeds raw BTF (the same bytes as /sys/kernel/btf/vmlinux) inside
the Image, so both the stock Image and a freshly built Image can be compared
without vmlinux or pahole.
"""
import struct

BTF_MAGIC_HDR = b"\x9f\xeb\x01\x00\x18\x00\x00\x00"

KIND_INT, KIND_PTR, KIND_ARRAY, KIND_STRUCT, KIND_UNION, KIND_ENUM = 1, 2, 3, 4, 5, 6
KIND_FWD, KIND_TYPEDEF, KIND_VOLATILE, KIND_CONST, KIND_RESTRICT = 7, 8, 9, 10, 11
KIND_FUNC, KIND_FUNC_PROTO, KIND_VAR, KIND_DATASEC, KIND_FLOAT = 12, 13, 14, 15, 16
KIND_DECL_TAG, KIND_TYPE_TAG, KIND_ENUM64 = 17, 18, 19
_MODIFIERS = (KIND_TYPEDEF, KIND_VOLATILE, KIND_CONST, KIND_RESTRICT, KIND_TYPE_TAG)


def extract_from_image(image: bytes) -> bytes:
    """Returns the raw BTF blob embedded in an arm64 kernel Image."""
    start = image.find(BTF_MAGIC_HDR)
    if start < 0:
        raise ValueError("no BTF header found in Image")
    _, _, _, hdr_len, _, _, str_off, str_len = struct.unpack_from("<HBBIIIII", image, start)
    return image[start:start + hdr_len + str_off + str_len]


class BTF:
    def __init__(self, data: bytes):
        magic, _, _, hdr_len, type_off, type_len, str_off, _ = struct.unpack_from("<HBBIIIII", data, 0)
        if magic != 0xEB9F:
            raise ValueError("bad BTF magic")
        self._data = data
        self._str_base = hdr_len + str_off
        self.types = [None]
        pos, end = hdr_len + type_off, hdr_len + type_off + type_len
        while pos < end:
            name_off, info, size_or_type = struct.unpack_from("<III", data, pos)
            pos += 12
            kind, vlen, kflag = (info >> 24) & 0x1F, info & 0xFFFF, info >> 31
            t = {"name": self._str(name_off), "kind": kind, "size": size_or_type, "kflag": kflag}
            if kind == KIND_INT:
                pos += 4
            elif kind == KIND_ARRAY:
                t["elem"], _, t["nelems"] = struct.unpack_from("<III", data, pos)
                pos += 12
            elif kind in (KIND_STRUCT, KIND_UNION):
                members = []
                for _ in range(vlen):
                    m_name, m_type, m_off = struct.unpack_from("<III", data, pos)
                    pos += 12
                    if kflag:
                        members.append((self._str(m_name), m_type, m_off & 0xFFFFFF, m_off >> 24))
                    else:
                        members.append((self._str(m_name), m_type, m_off, 0))
                t["members"] = members
            elif kind == KIND_ENUM:
                vals = []
                for _ in range(vlen):
                    v_name, v_val = struct.unpack_from("<Ii", data, pos)
                    pos += 8
                    vals.append((self._str(v_name), v_val))
                t["values"] = vals
            elif kind == KIND_ENUM64:
                vals = []
                for _ in range(vlen):
                    v_name, lo, hi = struct.unpack_from("<III", data, pos)
                    pos += 12
                    vals.append((self._str(v_name), (hi << 32) | lo))
                t["values"] = vals
            elif kind == KIND_FUNC_PROTO:
                pos += 8 * vlen
            elif kind == KIND_VAR:
                pos += 4
            elif kind == KIND_DATASEC:
                pos += 12 * vlen
            elif kind == KIND_DECL_TAG:
                pos += 4
            self.types.append(t)

    def _str(self, off: int) -> str:
        base = self._str_base + off
        return self._data[base:self._data.index(b"\0", base)].decode()

    def type_name(self, tid: int, depth: int = 0) -> str:
        if tid == 0:
            return "void"
        t = self.types[tid]
        k = t["kind"]
        if k in _MODIFIERS:
            return self.type_name(t["size"], depth)
        if k == KIND_PTR:
            return self.type_name(t["size"], depth) + "*"
        if k == KIND_ARRAY:
            return f"{self.type_name(t['elem'], depth)}[{t['nelems']}]"
        if k in (KIND_STRUCT, KIND_UNION) and not t["name"] and depth < 8:
            return self.layout(tid, depth + 1)
        if k == KIND_FWD:
            # A forward declaration and the full definition name the same
            # type; which one BTF records depends on which CUs got compiled.
            return ("union " if t["kflag"] else "struct ") + t["name"]
        prefix = {KIND_STRUCT: "struct ", KIND_UNION: "union ", KIND_ENUM: "enum ",
                  KIND_ENUM64: "enum "}.get(k, "")
        return prefix + (t["name"] or f"<anon kind {k}>")

    def layout(self, tid: int, depth: int = 0) -> str:
        """Stable textual layout of a struct/union: size plus every member's
        name, bit offset, bitfield size and type. Anonymous nested aggregates
        are expanded recursively so that hidden shifts are caught too."""
        t = self.types[tid]
        kw = "struct" if t["kind"] == KIND_STRUCT else "union"
        parts = [f"{m_name}@{m_off}:{m_bits}:{self.type_name(m_type, depth)}"
                 for (m_name, m_type, m_off, m_bits) in t["members"]]
        return f"{kw}{{size={t['size']};" + ";".join(parts) + "}"

    def named_aggregates(self) -> dict:
        out = {}
        for tid, t in enumerate(self.types):
            if t and t["kind"] in (KIND_STRUCT, KIND_UNION) and t["name"]:
                key = ("struct " if t["kind"] == KIND_STRUCT else "union ") + t["name"]
                out.setdefault(key, set()).add(self.layout(tid))
        return out

    def named_enums(self) -> dict:
        out = {}
        for t in self.types[1:]:
            if t["kind"] in (KIND_ENUM, KIND_ENUM64) and t["name"]:
                out.setdefault("enum " + t["name"], set()).add(
                    f"size={t['size']};" + ";".join(f"{n}={v}" for n, v in t["values"]))
        return out

    def members(self, struct_name: str):
        """Yields (name, byte_offset, size_bytes, type) for a named struct."""
        for tid, t in enumerate(self.types):
            if t and t["kind"] == KIND_STRUCT and t["name"] == struct_name:
                for (m_name, m_type, m_off, _) in t["members"]:
                    yield m_name, m_off // 8, self.sizeof(m_type), self.type_name(m_type)
                return

    def sizeof(self, tid: int) -> int:
        if tid == 0:
            return 0
        t = self.types[tid]
        k = t["kind"]
        if k in _MODIFIERS:
            return self.sizeof(t["size"])
        if k == KIND_PTR:
            return 8
        if k == KIND_ARRAY:
            return self.sizeof(t["elem"]) * t["nelems"]
        if k in (KIND_INT, KIND_STRUCT, KIND_UNION, KIND_ENUM, KIND_ENUM64, KIND_FLOAT):
            return t["size"]
        return 0
