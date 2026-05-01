#!/usr/bin/env python3
"""Generate racelink/racelink_proto_auto.py from the shared C++ header racelink_proto.h.

Why this exists:
- Master + Node share racelink_proto.h as the protocol source of truth.
- The RotorHazard host plugin (Python) should not duplicate protocol rules.

This generator extracts:
- PROTO_VER_* constants
- DIR_* constants + helpers (make_type, type_dir, type_base, flip_dir)
- Enums: Opcode7, RespPolicy, AckStatus, OffsetMode
- Sizes of packed structs (by summing field sizes)
- PacketRule registry (RULES[]), plus RULES_BY_OPCODE7 and find_rule()

Constraints:
- This is a pragmatic parser for the subset of C used in racelink_proto.h.
  It assumes __attribute__((packed)) for the payload structs.

Usage:
  ./gen_racelink_proto_py.py                     # reads ./racelink_proto.h, writes ./racelink/racelink_proto_auto.py
  ./gen_racelink_proto_py.py --in path --out path

"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import pathlib
import re
from typing import Dict, List, Optional, Tuple


_INT_RE = re.compile(r"^(0x[0-9A-Fa-f]+|\d+)$")


def _parse_int(token: str) -> int:
    token = token.strip()
    if token.startswith("0x") or token.startswith("0X"):
        return int(token, 16)
    return int(token, 10)


def _strip_comments(s: str) -> str:
    # remove // comments
    s = re.sub(r"//.*?$", "", s, flags=re.M)
    # remove /* ... */ comments
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return s


def _extract_static_u8(h: str, name: str) -> Optional[int]:
    m = re.search(rf"static\s+const\s+uint8_t\s+{re.escape(name)}\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;", h)
    if not m:
        return None
    return _parse_int(m.group(1))


def _extract_static_u8_prefix(h: str, prefix: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for m in re.finditer(r"static\s+const\s+uint8_t\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;", h):
        name, val = m.group(1), m.group(2)
        if not name.startswith(prefix):
            continue
        out[name] = _parse_int(val)
    return out


def _extract_enum(h: str, enum_name: str) -> Dict[str, int]:
    """Extract enum entries NAME = VALUE from 'enum <name> : uint8_t { ... };'."""
    m = re.search(rf"enum\s+{re.escape(enum_name)}\s*:\s*uint8_t\s*\{{(.*?)\}}\s*;", h, flags=re.S)
    if not m:
        return {}
    block = _strip_comments(m.group(1))
    out: Dict[str, int] = {}
    for nm, val in re.findall(r"\b([A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", block):
        out[nm] = _parse_int(val)
    return out


def _extract_body_max(h: str) -> Optional[int]:
    m = re.search(r"static\s+const\s+uint8_t\s+BODY_MAX\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;", h)
    if not m:
        return None
    return _parse_int(m.group(1))


_C_TYPE_SIZES = {
    "uint8_t": 1,
    "int8_t": 1,
    "char": 1,
    "uint16_t": 2,
    "int16_t": 2,
    "uint32_t": 4,
    "int32_t": 4,
}


def _parse_struct_field(struct_name: str, stmt: str) -> Tuple[str, str, int]:
    fm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)(\s*\[\s*(\d+)\s*\])?$", stmt)
    if not fm:
        fm = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*(\d+)\s*\]$", stmt)
        if not fm:
            raise ValueError(f"Unsupported struct field syntax in {struct_name}: {stmt!r}")
        ctype = fm.group(1)
        field_name = fm.group(2)
        arr_n = int(fm.group(3))
    else:
        ctype = fm.group(1)
        field_name = fm.group(2)
        arr_n = int(fm.group(4)) if fm.group(4) else 1

    if ctype not in _C_TYPE_SIZES:
        raise ValueError(f"Unsupported C type in packed struct {struct_name}: {ctype}")
    return field_name, ctype, arr_n


def _extract_packed_struct_defs(h: str) -> Dict[str, List[Tuple[str, str, int]]]:
    """Return packed struct field layouts as {StructName: [(field, ctype, array_len), ...]}."""
    out: Dict[str, List[Tuple[str, str, int]]] = {}

    # Capture packed structs: struct __attribute__((packed)) Name { ... };
    for m in re.finditer(
        r"struct\s+__attribute__\(\(packed\)\)\s+(\w+)\s*\{(.*?)\}\s*;",
        h,
        flags=re.S,
    ):
        name = m.group(1)
        body = _strip_comments(m.group(2))
        fields: List[Tuple[str, str, int]] = []
        # Split into statements by ';'
        for stmt in body.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            fields.append(_parse_struct_field(name, stmt))

        out[name] = fields

    return out


def _struct_sizes_from_defs(struct_defs: Dict[str, List[Tuple[str, str, int]]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for name, fields in struct_defs.items():
        out[name] = sum(_C_TYPE_SIZES[ctype] * arr_n for _, ctype, arr_n in fields)
    return out


@dataclasses.dataclass(frozen=True)
class PacketRulePy:
    opcode7: int
    req_dir: int
    policy: int
    rsp_opcode7: int
    req_len: int
    rsp_len: int
    name: str


def _split_top_level_commas(s: str) -> List[str]:
    """Split a C initializer list entry into tokens by top-level commas."""
    out: List[str] = []
    cur: List[str] = []
    paren = 0
    angle = 0
    in_str = False
    esc = False

    for ch in s:
        if in_str:
            cur.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            cur.append(ch)
            continue

        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)

        if ch == "," and paren == 0 and angle == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)

    if cur:
        out.append("".join(cur).strip())

    return out


def _extract_rules(h: str, constants: Dict[str, int], struct_sizes: Dict[str, int]) -> List[PacketRulePy]:
    # isolate RULES[] initializer
    m = re.search(r"static\s+constexpr\s+PacketRule\s+RULES\[\]\s*=\s*\{", h)
    if not m:
        return []

    start = m.end()  # right after '{'
    # Find matching '};' for the initializer list (brace matching)
    brace = 1
    i = start
    while i < len(h) and brace > 0:
        ch = h[i]
        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
        i += 1

    init_block = h[start : i - 1]  # without the final '}'
    init_block = _strip_comments(init_block)

    # Extract each top-level { ... }
    rules: List[PacketRulePy] = []
    brace = 0
    entry_start = None
    for idx, ch in enumerate(init_block):
        if ch == "{":
            if brace == 0:
                entry_start = idx + 1
            brace += 1
        elif ch == "}":
            brace -= 1
            if brace == 0 and entry_start is not None:
                entry = init_block[entry_start:idx].strip()
                entry_start = None
                if not entry:
                    continue
                toks = _split_top_level_commas(entry)
                if len(toks) != 7:
                    raise ValueError(f"Unexpected RULES[] entry token count ({len(toks)}): {toks}")

                def resolve(tok: str) -> int:
                    tok = tok.strip()
                    if tok.startswith("SZ<") and tok.endswith(">()"):
                        tname = tok[len("SZ<") : -len(">()")].strip()
                        if tname not in struct_sizes:
                            raise KeyError(f"Unknown struct in SZ<>: {tname}")
                        return int(struct_sizes[tname])
                    if _INT_RE.match(tok):
                        return _parse_int(tok)
                    if tok in constants:
                        return int(constants[tok])
                    # allow 0 literal without match (already covered) and allow empty
                    raise KeyError(f"Unknown token in RULES[]: {tok}")

                opcode7 = resolve(toks[0])
                req_dir = resolve(toks[1])
                policy = resolve(toks[2])
                rsp_opcode7 = resolve(toks[3])
                req_len = resolve(toks[4])
                rsp_len = resolve(toks[5])

                name_tok = toks[6].strip()
                sm = re.match(r'^"(.*)"$', name_tok)
                if not sm:
                    raise ValueError(f"Rule name is not a string literal: {name_tok}")
                name = sm.group(1)

                rules.append(PacketRulePy(opcode7, req_dir, policy, rsp_opcode7, req_len, rsp_len, name))

    return rules


def _py_header() -> str:
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "# Auto-generated from racelink_proto.h by gen_racelink_proto_py.py\n"
        f"# Generated: {ts}\n"
        "# DO NOT EDIT THIS FILE BY HAND.\n\n"
    )


def generate(header_path: pathlib.Path, out_path: pathlib.Path) -> None:
    h = header_path.read_text(encoding="utf-8", errors="ignore")

    # constants
    constants: Dict[str, int] = {}

    # version + body_max
    for k in ("PROTO_VER_MAJOR", "PROTO_VER_MINOR"):
        v = _extract_static_u8(h, k)
        if v is not None:
            constants[k] = v
    bm = _extract_body_max(h)
    if bm is not None:
        constants["BODY_MAX"] = bm

    # DIR_* constants
    constants.update(_extract_static_u8_prefix(h, "DIR_"))

    # USB framing event types + reason codes + host->gateway commands
    # (Batch B). These don't travel on the LoRa wire but the host transport
    # mirrors them; keep them in lockstep with the C++ header.
    constants.update(_extract_static_u8_prefix(h, "EV_"))
    constants.update(_extract_static_u8_prefix(h, "TX_REJECT_"))
    constants.update(_extract_static_u8_prefix(h, "GW_CMD_"))

    # enums
    constants.update(_extract_enum(h, "Opcode7"))
    constants.update(_extract_enum(h, "RespPolicy"))
    constants.update(_extract_enum(h, "AckStatus"))
    constants.update(_extract_enum(h, "OffsetMode"))
    constants.update(_extract_enum(h, "GatewayState"))

    # packed struct layouts + sizes
    struct_defs = _extract_packed_struct_defs(h)
    struct_sizes = _struct_sizes_from_defs(struct_defs)

    # rules
    rules = _extract_rules(h, constants=constants, struct_sizes=struct_sizes)

    # emit python
    lines: List[str] = []
    lines.append(_py_header())

    # constants (sorted, but keep a few first)
    for key in ("PROTO_VER_MAJOR", "PROTO_VER_MINOR", "BODY_MAX", "DIR_M2N", "DIR_N2M"):
        if key in constants:
            lines.append(f"{key} = {constants[key]}")
    # the rest
    for key in sorted(constants.keys()):
        if key in ("PROTO_VER_MAJOR", "PROTO_VER_MINOR", "BODY_MAX", "DIR_M2N", "DIR_N2M"):
            continue
        lines.append(f"{key} = {constants[key]}")

    lines.append("")

    # helpers (mirror header)
    lines.append("def type_dir(t: int) -> int: return t & 0x80")
    lines.append("def type_base(t: int) -> int: return t & 0x7F")
    lines.append("def flip_dir(t: int) -> int: return t ^ 0x80")
    lines.append("def make_type(dir_: int, opcode7: int) -> int: return (dir_ | (opcode7 & 0x7F))")
    lines.append("")

    # struct sizes
    lines.append("# Packed struct sizes (bytes)")
    for sn in sorted(struct_sizes.keys()):
        lines.append(f"SZ_{sn} = {struct_sizes[sn]}")
    lines.append("")

    lines.append("# Packed struct field layouts extracted from racelink_proto.h")
    lines.append("STRUCT_FIELDS = {")
    for sn in sorted(struct_defs.keys()):
        lines.append(f"    {sn!r}: [")
        for field_name, ctype, arr_n in struct_defs[sn]:
            lines.append(f"        ({field_name!r}, {ctype!r}, {arr_n}),")
        lines.append("    ],")
    lines.append("}")
    lines.append("")

    # PacketRule
    lines.append("from dataclasses import dataclass")
    lines.append("from typing import Dict, List, Optional")
    lines.append("")
    lines.append("@dataclass(frozen=True)")
    lines.append("class PacketRule:")
    lines.append("    opcode7: int")
    lines.append("    req_dir: int")
    lines.append("    policy: int")
    lines.append("    rsp_opcode7: int")
    lines.append("    req_len: int")
    lines.append("    rsp_len: int")
    lines.append("    name: str")
    lines.append("")

    lines.append("RULES: List[PacketRule] = [")
    for r in rules:
        lines.append(
            "    PacketRule(" + ", ".join(
                [
                    str(r.opcode7),
                    str(r.req_dir),
                    str(r.policy),
                    str(r.rsp_opcode7),
                    str(r.req_len),
                    str(r.rsp_len),
                    repr(r.name),
                ]
            ) + "),"
        )
    lines.append("]")
    lines.append("")

    lines.append("RULES_BY_OPCODE7: Dict[int, PacketRule] = {r.opcode7: r for r in RULES}")
    lines.append("")
    lines.append("def find_rule(opcode7: int) -> Optional[PacketRule]:")
    lines.append("    return RULES_BY_OPCODE7.get(int(opcode7) & 0x7F)")
    lines.append("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", default="racelink_proto.h", help="path to racelink_proto.h")
    ap.add_argument("--out", dest="out_path", default="racelink/racelink_proto_auto.py", help="output python module")
    args = ap.parse_args()

    header_path = pathlib.Path(args.in_path)
    out_path = pathlib.Path(args.out_path)

    generate(header_path, out_path)
    print(f"Wrote {out_path} from {header_path}")


if __name__ == "__main__":
    main()
