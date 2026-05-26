"""LegoMCP server: ModelState, LDraw I/O, validation, and all MCP tools.

Coordinate convention (LDraw):
    - Right-handed, **-Y is up**.
    - 1 stud width = 20 LDU. 1 plate height = 8 LDU. 1 brick height = 24 LDU.
    - Part origins are at the geometric center of the bottom face.
      So a brick at y=0 sits on the ground; a brick stacked on it is at y=-24.

Rotation:
    - Six canonical orientations: identity, rot90y, rot180y, rot270y, rot90x, rot90z.
    - The full LDraw 3x3 matrix is computed from the name on export.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from lego_mcp.parts import (
    BUILTIN_PARTS,
    COLORS,
    Part,
    color_rgb,
    load_library_index,
    resolve_color,
    search as search_parts,
)

log = logging.getLogger("lego_mcp")

# ---------------------------------------------------------------------------
# Rotation matrices (row-major, as LDraw stores them)
# ---------------------------------------------------------------------------

Matrix = tuple[float, float, float, float, float, float, float, float, float]

ROTATIONS: dict[str, Matrix] = {
    "identity": (1, 0, 0,  0, 1, 0,  0, 0, 1),
    "rot90y":   (0, 0, 1,  0, 1, 0, -1, 0, 0),
    "rot180y":  (-1, 0, 0, 0, 1, 0,  0, 0, -1),
    "rot270y":  (0, 0, -1, 0, 1, 0,  1, 0, 0),
    "rot90x":   (1, 0, 0,  0, 0, -1, 0, 1, 0),
    "rot90z":   (0, -1, 0, 1, 0, 0,  0, 0, 1),
}


def matrix_apply(m: Matrix, v: tuple[float, float, float]) -> tuple[float, float, float]:
    a, b, c, d, e, f, g, h, i = m
    x, y, z = v
    return (a * x + b * y + c * z,
            d * x + e * y + f * z,
            g * x + h * y + i * z)


def resolve_rotation(name: str) -> Matrix:
    key = name.strip().lower()
    if key not in ROTATIONS:
        raise ValueError(f"Unknown rotation {name!r}. Choose one of: {', '.join(ROTATIONS)}")
    return ROTATIONS[key]


# ---------------------------------------------------------------------------
# Model state
# ---------------------------------------------------------------------------

@dataclass
class PartInstance:
    instance_id: str
    part_id: str       # LDraw .dat stem (e.g. "3001")
    color: int         # LDraw color ID
    x: float
    y: float
    z: float
    rotation: str = "identity"
    subassembly: str = "main"   # named group this part belongs to


@dataclass
class Op:
    """A reversible mutation. Both directions are O(1)."""
    kind: str           # "add" | "remove" | "move" | "rotate"
    instance_id: str
    data: dict[str, Any]


@dataclass
class ModelState:
    name: str = "untitled"
    parts: dict[str, PartInstance] = field(default_factory=dict)
    _next_id: int = 1
    current_subassembly: str = "main"
    notes: dict[str, str] = field(default_factory=dict)
    # Op-based undo/redo: O(1) per mutation regardless of model size.
    _undo: list[Op] = field(default_factory=list)
    _redo: list[Op] = field(default_factory=list)
    _checkpoints: dict[str, "ModelState"] = field(default_factory=dict)

    def new_id(self) -> str:
        i = str(self._next_id)
        self._next_id += 1
        return i


STATE = ModelState()
PART_INDEX: dict[str, Part] = dict(BUILTIN_PARTS)
_LIBRARY_LOADED = False
UNDO_LIMIT = 500


def _ensure_library_loaded() -> None:
    """Load the full LDraw library the first time it's needed. Idempotent."""
    global PART_INDEX, _LIBRARY_LOADED
    if _LIBRARY_LOADED:
        return
    _LIBRARY_LOADED = True
    PART_INDEX = load_library_index()


def _record(op: Op) -> None:
    STATE._undo.append(op)
    STATE._redo.clear()
    if len(STATE._undo) > UNDO_LIMIT:
        STATE._undo = STATE._undo[-UNDO_LIMIT:]


def _apply_forward(op: Op) -> None:
    if op.kind == "add":
        STATE.parts[op.instance_id] = deepcopy(op.data["inst"])
    elif op.kind == "remove":
        STATE.parts.pop(op.instance_id, None)
    elif op.kind == "move":
        inst = STATE.parts[op.instance_id]
        inst.x, inst.y, inst.z = op.data["new_pos"]
    elif op.kind == "rotate":
        STATE.parts[op.instance_id].rotation = op.data["new_rot"]


def _apply_inverse(op: Op) -> None:
    if op.kind == "add":
        STATE.parts.pop(op.instance_id, None)
    elif op.kind == "remove":
        STATE.parts[op.instance_id] = deepcopy(op.data["inst"])
    elif op.kind == "move":
        inst = STATE.parts[op.instance_id]
        inst.x, inst.y, inst.z = op.data["old_pos"]
    elif op.kind == "rotate":
        STATE.parts[op.instance_id].rotation = op.data["old_rot"]


def _require_part(part_id: str) -> Part:
    # Load the full library on first lookup (idempotent + cache-backed).
    # Without this, built-in parts would short-circuit and we'd never get the
    # real LDraw stud positions / catalog metadata.
    _ensure_library_loaded()
    p = PART_INDEX.get(part_id) or PART_INDEX.get(part_id.lower())
    if not p:
        raise ValueError(
            f"Unknown part {part_id!r}. Use search_parts() to find one, or run "
            "`lego-mcp install-library` to get the full LDraw catalog."
        )
    return p


# ---------------------------------------------------------------------------
# AABB / collision
# ---------------------------------------------------------------------------

def part_aabb_world(inst: PartInstance, part: Part) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """World-space AABB after rotation + translation. Origin = center of bottom face."""
    w, h, d = part.width, part.height, part.depth
    # Local AABB corners. Height extends in -Y because -Y is up.
    local = [(sx, sy, sz)
             for sx in (-w / 2, w / 2)
             for sy in (-h, 0)
             for sz in (-d / 2, d / 2)]
    m = resolve_rotation(inst.rotation)
    rotated = [matrix_apply(m, c) for c in local]
    xs = [p[0] for p in rotated]
    ys = [p[1] for p in rotated]
    zs = [p[2] for p in rotated]
    return ((inst.x + min(xs), inst.y + min(ys), inst.z + min(zs)),
            (inst.x + max(xs), inst.y + max(ys), inst.z + max(zs)))


def aabbs_overlap(a: tuple, b: tuple, tolerance: float = 0.5) -> bool:
    """True if AABBs intersect with more than `tolerance` LDU on every axis."""
    (amin, amax), (bmin, bmax) = a, b
    for i in range(3):
        if amin[i] >= bmax[i] - tolerance or bmin[i] >= amax[i] - tolerance:
            return False
    return True


def xz_overlap_area(a: tuple, b: tuple) -> float:
    """Overlap area of two AABBs in the XZ plane, in LDU²."""
    (amin, amax), (bmin, bmax) = a, b
    dx = min(amax[0], bmax[0]) - max(amin[0], bmin[0])
    dz = min(amax[2], bmax[2]) - max(amin[2], bmin[2])
    return max(0.0, dx) * max(0.0, dz)


GROUND_Y = 0.0          # LDU. The "ground plane" where unsupported parts can rest.
SUPPORT_TOL = 0.5       # LDU. Vertical gap tolerance for considering "touching".
# A real LEGO connection needs at least one stud's worth of overlap (the stud
# is what mates with the part above's anti-stud). One stud's footprint = 20x20
# LDU = 400 LDU². If the XZ-interface area is below this, the parts touch but
# don't actually clutch — physically equivalent to a floating brick.
MIN_SUPPORT_AREA = 400.0


def _check_supported(inst_aabb: tuple, neighbor_aabbs: list[tuple]) -> bool:
    """True if the part has any valid LEGO connection.

    A connection counts as valid if there's at least one stud's worth of
    XZ overlap (>= MIN_SUPPORT_AREA) at the part's TOP face (held from above)
    OR its BOTTOM face (sits on something below). Or the part is grounded.

    Note: this is a local connectivity check, not a global reachability one.
    A small connected island floating in space won't trip the floating-part
    detector because each of its parts is locally "connected" to its neighbors.
    A full reachability check (does this connected component touch the ground?)
    is a future improvement — for now, the LLM can spot disconnected islands
    by render + spatial reasoning.
    """
    (_, inst_top_y, _), (_, inst_bottom_y, _) = inst_aabb
    if abs(inst_bottom_y - GROUND_Y) < SUPPORT_TOL:
        return True
    for n in neighbor_aabbs:
        (_, n_top_y, _), (_, n_bottom_y, _) = n
        # Connection from below: neighbor's top face matches our bottom face.
        if (abs(n_top_y - inst_bottom_y) < SUPPORT_TOL
                and xz_overlap_area(inst_aabb, n) >= MIN_SUPPORT_AREA):
            return True
        # Connection from above (SNOT / hanging brick): neighbor's bottom face
        # matches our top face.
        if (abs(n_bottom_y - inst_top_y) < SUPPORT_TOL
                and xz_overlap_area(inst_aabb, n) >= MIN_SUPPORT_AREA):
            return True
    return False


# ---------------------------------------------------------------------------
# LDraw read / write
# ---------------------------------------------------------------------------

_TYPE1_RE = re.compile(
    r"^\s*1\s+(-?\d+)\s+"
    r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+"
    r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+"
    r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+"
    r"(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+"
    r"(.+\.dat)\s*$",
    re.IGNORECASE,
)


def _matrix_to_rotation_name(m: Matrix) -> str:
    """Best-effort reverse lookup of a name for a matrix; falls back to 'identity' (with a warning) if exotic."""
    for name, ref in ROTATIONS.items():
        if all(abs(a - b) < 1e-3 for a, b in zip(m, ref)):
            return name
    log.warning("Imported matrix doesn't match any canonical rotation; storing as identity.")
    return "identity"


def _emit_inst_line(inst: PartInstance) -> str:
    m = resolve_rotation(inst.rotation)
    return (f"1 {inst.color} {inst.x:g} {inst.y:g} {inst.z:g} "
            + " ".join(f"{v:g}" for v in m)
            + f" {inst.part_id}.dat")


def emit_ldr(state: ModelState) -> str:
    """Emit a single-file .ldr body (no FILE markers). Flattens all subassemblies."""
    out = [f"0 {state.name}", "0 Generated by LegoMCP", "0 Name: " + state.name + ".ldr", ""]
    for inst in state.parts.values():
        out.append(_emit_inst_line(inst))
    return "\n".join(out) + "\n"


def emit_mpd(state: ModelState) -> str:
    """Emit a multi-block .mpd: one 0 FILE block per subassembly tag.

    Main block contains "main"-tagged parts plus type-1 references to each
    other subassembly with identity transform (parts within subs are already
    at absolute positions, so identity is the right transform).
    """
    by_sub: dict[str, list[PartInstance]] = {}
    for inst in state.parts.values():
        by_sub.setdefault(inst.subassembly, []).append(inst)

    subs = sorted(n for n in by_sub if n != "main")
    out: list[str] = []
    out.append("0 FILE main.ldr")
    out.append(f"0 {state.name}")
    out.append("0 Generated by LegoMCP")
    out.append("0 Name: main.ldr")
    out.append("")
    for inst in by_sub.get("main", []):
        out.append(_emit_inst_line(inst))
    for sub_name in subs:
        # Reference each subassembly at identity. Subassembly parts hold absolute coords.
        out.append(f"1 16 0 0 0 1 0 0 0 1 0 0 0 1 {sub_name}.ldr")
    out.append("0 NOFILE")
    for sub_name in subs:
        out.append("")
        out.append(f"0 FILE {sub_name}.ldr")
        out.append(f"0 Subassembly: {sub_name}")
        out.append("")
        for inst in by_sub[sub_name]:
            out.append(_emit_inst_line(inst))
        out.append("0 NOFILE")
    return "\n".join(out) + "\n"


def parse_ldr_text(text: str) -> list[PartInstance]:
    """Parse .ldr or .mpd text into PartInstance records.

    Multi-block MPDs are handled: parts inside each `0 FILE <name>.ldr` block
    get tagged with that block's name (stem). The first block is "main".

    Sub-file references in main (type-1 lines whose filename matches a defined
    block) are expanded: the referenced block's parts are added with the
    reference's transform composed onto each part's position. This is needed
    for round-tripping MPDs we emitted and for importing models others built
    with sub-file references.

    Caller assigns fresh instance IDs (we use sequential placeholders here).
    """
    # Pass 1: split into blocks. Anything before the first 0 FILE belongs to main.
    blocks: dict[str, list[str]] = {}
    order: list[str] = []
    current = "main"
    blocks[current] = []
    order.append(current)
    for raw in text.splitlines():
        stripped = raw.strip()
        low = stripped.lower()
        if low.startswith("0 file "):
            name = stripped[len("0 FILE "):].strip()
            name = name.rsplit(".", 1)[0]  # strip .ldr
            if not order or order != [name]:
                current = name if name != "main.ldr" else "main"
                if order == ["main"] and not blocks["main"]:
                    # First FILE marker — its block becomes "main".
                    current = name
                    order = [current]
                    blocks.pop("main", None)
                else:
                    if current not in blocks:
                        order.append(current)
                blocks.setdefault(current, [])
            continue
        if low.startswith("0 nofile"):
            continue
        blocks.setdefault(current, []).append(raw)

    main_name = order[0]
    block_names = set(blocks.keys())

    def parse_block(name: str, transform: tuple[float, ...]) -> list[PartInstance]:
        """Recursively parse a block; sub-file refs to known blocks get expanded
        with composed transforms. Cycles are guarded by depth limit."""
        results: list[PartInstance] = []
        for raw in blocks.get(name, []):
            m = _TYPE1_RE.match(raw)
            if not m:
                continue
            color = int(m.group(1))
            x, y, z = float(m.group(2)), float(m.group(3)), float(m.group(4))
            mat = tuple(float(m.group(i)) for i in range(5, 14))
            part_path = m.group(14).strip()
            part_stem = Path(part_path).stem
            # If this references another block in the MPD, expand recursively.
            if part_stem in block_names and part_stem != name:
                composed = _compose_transform(transform, (x, y, z) + mat)
                sub_parts = parse_block(part_stem, composed)
                for sp in sub_parts:
                    sp.subassembly = part_stem
                results.extend(sub_parts)
                continue
            # Plain part reference. Apply the block's accumulated transform.
            world = _compose_transform(transform, (x, y, z) + mat)
            rot_name = _matrix_to_rotation_name(world[3:12])  # type: ignore[arg-type]
            results.append(PartInstance(
                instance_id="0",  # caller will assign
                part_id=part_stem,
                color=color,
                x=world[0], y=world[1], z=world[2],
                rotation=rot_name,
                subassembly=name,
            ))
        return results

    identity = (0.0, 0.0, 0.0,  1.0, 0.0, 0.0,  0.0, 1.0, 0.0,  0.0, 0.0, 1.0)
    parts = parse_block(main_name, identity)
    seen_subs = {p.subassembly for p in parts}
    # Also import parts from blocks that weren't referenced by main — tagged
    # with their own block name. Keeps us forgiving with hand-edited MPDs.
    for block_name in order[1:]:
        if block_name in seen_subs:
            continue
        for p in parse_block(block_name, identity):
            p.subassembly = block_name
            parts.append(p)
    return parts


def _compose_transform(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    """Compose two LDraw transforms (12-tuples: pos + 3x3 matrix). a applied first, then b."""
    ax, ay, az = a[0], a[1], a[2]
    am = a[3:12]
    bx, by, bz = b[0], b[1], b[2]
    bm = b[3:12]
    nx = ax + am[0]*bx + am[1]*by + am[2]*bz
    ny = ay + am[3]*bx + am[4]*by + am[5]*bz
    nz = az + am[6]*bx + am[7]*by + am[8]*bz
    def row(i: int) -> tuple[float, float, float]:
        return (
            am[i*3]*bm[0] + am[i*3+1]*bm[3] + am[i*3+2]*bm[6],
            am[i*3]*bm[1] + am[i*3+1]*bm[4] + am[i*3+2]*bm[7],
            am[i*3]*bm[2] + am[i*3+1]*bm[5] + am[i*3+2]*bm[8],
        )
    return (nx, ny, nz) + row(0) + row(1) + row(2)


# ---------------------------------------------------------------------------
# Serialisation helpers for tool responses
# ---------------------------------------------------------------------------

def _inst_dict(inst: PartInstance) -> dict[str, Any]:
    d = asdict(inst)
    p = PART_INDEX.get(inst.part_id)
    if p:
        d["part_name"] = p.name
    return d


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("lego-mcp")


def _reset_state(name: str, keep_checkpoints: bool = False) -> None:
    """Reset STATE in place (don't rebind), so external references stay live."""
    STATE.name = name
    STATE.parts.clear()
    STATE._next_id = 1
    STATE.current_subassembly = "main"
    STATE.notes.clear()
    STATE._undo.clear()
    STATE._redo.clear()
    if not keep_checkpoints:
        STATE._checkpoints.clear()


@mcp.tool()
def create_model(name: str = "untitled") -> dict[str, Any]:
    """Start a fresh, empty model. Clears parts, undo, redo, and checkpoints."""
    _reset_state(name)
    return {"ok": True, "name": name, "parts": 0}


@mcp.tool()
def add_part(
    part_id: str,
    color: str | int = "light_bluish_gray",
    x: float = 0,
    y: float = 0,
    z: float = 0,
    rotation: str = "identity",
    strict: bool = False,
) -> dict[str, Any]:
    """Place a brick. Returns the new instance_id.

    Args:
        part_id: LDraw part number (e.g. "3001" for a 2x4 brick).
        color: Color name (e.g. "red") or LDraw color ID (e.g. 4).
        x, y, z: Position in LDU. Origin = center of part's bottom face. **-Y is up.**
        rotation: One of identity, rot90y, rot180y, rot270y, rot90x, rot90z.
        strict: If True, REJECT the placement when it would overlap an existing
            part or have no support below (no baseplate / brick beneath, no
            ground at y=0). Use this when you want the model to be physically
            buildable as you go, instead of catching problems later in
            validate_model.
    """
    part = _require_part(part_id)
    cid = resolve_color(color)
    resolve_rotation(rotation)  # validate name
    candidate = PartInstance(
        instance_id="_pending", part_id=part.part_id, color=cid,
        x=float(x), y=float(y), z=float(z), rotation=rotation.lower(),
        subassembly=STATE.current_subassembly,
    )
    if strict:
        cand_aabb = part_aabb_world(candidate, part)
        neighbor_aabbs: list[tuple] = []
        for other in STATE.parts.values():
            other_part = PART_INDEX.get(other.part_id)
            if other_part is None:
                continue
            other_aabb = part_aabb_world(other, other_part)
            if aabbs_overlap(cand_aabb, other_aabb):
                raise ValueError(
                    f"strict: would collide with instance {other.instance_id} "
                    f"({other.part_id} at {other.x},{other.y},{other.z})"
                )
            neighbor_aabbs.append(other_aabb)
        if not _check_supported(cand_aabb, neighbor_aabbs):
            raise ValueError(
                f"strict: no support below at ({x},{y},{z}). Need a baseplate / "
                f"brick at top face y={cand_aabb[1][1]}, or y must be at ground (0)."
            )
    inst_id = STATE.new_id()
    candidate.instance_id = inst_id
    STATE.parts[inst_id] = candidate
    _record(Op("add", inst_id, {"inst": deepcopy(candidate)}))
    return {"ok": True, "instance_id": inst_id, "part": _inst_dict(candidate)}


@mcp.tool()
def remove_part(instance_id: str) -> dict[str, Any]:
    """Remove a part by its instance ID. Undoable."""
    inst = STATE.parts.get(instance_id)
    if inst is None:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    _record(Op("remove", instance_id, {"inst": deepcopy(inst)}))
    STATE.parts.pop(instance_id)
    return {"ok": True, "removed": instance_id}


@mcp.tool()
def move_part(instance_id: str, x: float, y: float, z: float) -> dict[str, Any]:
    """Reposition a part to a new (x,y,z) in LDU. Undoable."""
    inst = STATE.parts.get(instance_id)
    if inst is None:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    old_pos = (inst.x, inst.y, inst.z)
    new_pos = (float(x), float(y), float(z))
    inst.x, inst.y, inst.z = new_pos
    _record(Op("move", instance_id, {"old_pos": old_pos, "new_pos": new_pos}))
    return {"ok": True, "part": _inst_dict(inst)}


@mcp.tool()
def rotate_part(instance_id: str, rotation: str) -> dict[str, Any]:
    """Change a part's rotation. Undoable."""
    inst = STATE.parts.get(instance_id)
    if inst is None:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    resolve_rotation(rotation)
    old_rot = inst.rotation
    new_rot = rotation.lower()
    inst.rotation = new_rot
    _record(Op("rotate", instance_id, {"old_rot": old_rot, "new_rot": new_rot}))
    return {"ok": True, "part": _inst_dict(inst)}


@mcp.tool()
def list_parts(limit: int = 200, subassembly: str | None = None) -> dict[str, Any]:
    """List parts in the model. Optional `subassembly` filter."""
    pool = list(STATE.parts.values())
    if subassembly:
        pool = [p for p in pool if p.subassembly == subassembly]
    items = pool[:limit]
    return {
        "model": STATE.name,
        "subassembly_filter": subassembly,
        "total": len(pool),
        "shown": len(items),
        "parts": [_inst_dict(i) for i in items],
    }


@mcp.tool()
def search_parts_tool(query: str, limit: int = 20) -> dict[str, Any]:
    """Search the active part catalog (built-in + LDraw library if installed)."""
    _ensure_library_loaded()
    hits = search_parts(PART_INDEX, query, limit)
    return {"query": query, "count": len(hits),
            "parts": [{"part_id": p.part_id, "name": p.name,
                       "width": p.width, "depth": p.depth, "height": p.height}
                      for p in hits]}


# Friendlier MCP name (the tool decorator uses the function name).
search_parts_tool.__name__ = "search_parts"  # type: ignore[attr-defined]


@mcp.tool()
def get_part_info(part_id: str) -> dict[str, Any]:
    """Get dimensions and name for a part_id."""
    p = _require_part(part_id)
    return {"part_id": p.part_id, "name": p.name,
            "width_ldu": p.width, "depth_ldu": p.depth, "height_ldu": p.height,
            "width_studs": round(p.width / 20, 2),
            "depth_studs": round(p.depth / 20, 2),
            "height_plates": round(p.height / 8, 2)}


@mcp.tool()
def list_colors() -> dict[str, Any]:
    """List supported color names and their LDraw IDs."""
    return {"colors": [{"name": name, "id": cid} for name, (cid, _) in COLORS.items()]}


@mcp.tool()
def find_connections(part_a_id: str, part_b_id: str,
                      full_nesting_only: bool = False) -> dict[str, Any]:
    """Enumerate every valid LEGO connection between two parts.

    Returns the list of relative placements where B stacks on A (and vice
    versa) with at least one stud-to-receptor mating. Set
    full_nesting_only=True to keep only placements where ALL of B's
    receptors mate (i.e. B's full footprint sits inside A's stud area).
    """
    from lego_mcp.connections import find_connections as _find
    a = _require_part(part_a_id)
    b = _require_part(part_b_id)
    return _find(a, b, full_nesting_only=full_nesting_only)


# ---------------------------------------------------------------------------
# Subassemblies (tag-based: every PartInstance has a subassembly name).
# Use these to organize complex builds (cathedral mechanisms, corner towers,
# spires, etc.) and to clone/mirror chunks of work without rebuilding them.
# ---------------------------------------------------------------------------

@mcp.tool()
def set_current_subassembly(name: str) -> dict[str, Any]:
    """Subsequent `add_part` calls tag parts with this subassembly name.

    The default subassembly is "main". Switch to a named one before building a
    sub-component, then switch back. No parts are moved; this only changes the
    tag applied to NEW parts.
    """
    STATE.current_subassembly = name.strip() or "main"
    return {"ok": True, "current_subassembly": STATE.current_subassembly}


@mcp.tool()
def list_subassemblies() -> dict[str, Any]:
    """List subassembly names + part counts in the current model."""
    counts: dict[str, int] = {}
    for inst in STATE.parts.values():
        counts[inst.subassembly] = counts.get(inst.subassembly, 0) + 1
    return {
        "current": STATE.current_subassembly,
        "subassemblies": [{"name": n, "parts": c}
                          for n, c in sorted(counts.items())],
    }


@mcp.tool()
def remove_subassembly(name: str) -> dict[str, Any]:
    """Delete every part tagged with this subassembly. Undoable (one op per part)."""
    victims = [i for i, p in STATE.parts.items() if p.subassembly == name]
    for iid in victims:
        inst = STATE.parts[iid]
        _record(Op("remove", iid, {"inst": deepcopy(inst)}))
        STATE.parts.pop(iid)
    return {"ok": True, "removed": len(victims), "subassembly": name}


@mcp.tool()
def clone_subassembly(src: str, dst: str,
                       x_offset: float = 0, y_offset: float = 0, z_offset: float = 0,
                       ) -> dict[str, Any]:
    """Duplicate every part of `src` into a new subassembly `dst`, with an offset.

    Use this to repeat a built structure (e.g. four identical corner towers).
    """
    if dst == src:
        raise ValueError("dst must differ from src")
    sources = [p for p in STATE.parts.values() if p.subassembly == src]
    if not sources:
        raise ValueError(f"subassembly {src!r} has no parts")
    cloned = 0
    for old in sources:
        inst_id = STATE.new_id()
        inst = PartInstance(
            instance_id=inst_id, part_id=old.part_id, color=old.color,
            x=old.x + x_offset, y=old.y + y_offset, z=old.z + z_offset,
            rotation=old.rotation, subassembly=dst,
        )
        STATE.parts[inst_id] = inst
        _record(Op("add", inst_id, {"inst": deepcopy(inst)}))
        cloned += 1
    return {"ok": True, "src": src, "dst": dst, "parts": cloned}


@mcp.tool()
def mirror_subassembly(src: str, dst: str, axis: str = "x",
                        plane_offset: float = 0) -> dict[str, Any]:
    """Mirror `src` across an axis plane into a new subassembly `dst`.

    Args:
        axis: "x" or "z". The plane perpendicular to this axis is the mirror.
        plane_offset: Position of the mirror plane on that axis (default 0).
            For instance, mirror across x = -200 means each new part has
            new.x = 2*(-200) - old.x.

    Note: rotations don't always mirror cleanly with our canonical-rotation
    system. Parts are kept with their original rotation; you may need to
    manually rotate the mirrored copy for asymmetric pieces.
    """
    if dst == src:
        raise ValueError("dst must differ from src")
    axis_lower = axis.lower()
    if axis_lower not in ("x", "z"):
        raise ValueError("axis must be 'x' or 'z'")
    sources = [p for p in STATE.parts.values() if p.subassembly == src]
    if not sources:
        raise ValueError(f"subassembly {src!r} has no parts")
    # Rotation flip table for mirroring around a vertical (Y) axis with plane
    # perpendicular to X or Z. This is a best-effort mapping.
    flip_table_x = {
        "identity": "rot180y",
        "rot180y": "identity",
        "rot90y": "rot270y",
        "rot270y": "rot90y",
        "rot90x": "rot90x",
        "rot90z": "rot90z",
    }
    flip_table_z = {
        "identity": "identity",
        "rot180y": "rot180y",
        "rot90y": "rot270y",
        "rot270y": "rot90y",
        "rot90x": "rot90x",
        "rot90z": "rot90z",
    }
    flip_table = flip_table_x if axis_lower == "x" else flip_table_z
    mirrored = 0
    for old in sources:
        inst_id = STATE.new_id()
        if axis_lower == "x":
            new_x = 2 * plane_offset - old.x
            new_y, new_z = old.y, old.z
        else:
            new_x, new_y = old.x, old.y
            new_z = 2 * plane_offset - old.z
        inst = PartInstance(
            instance_id=inst_id, part_id=old.part_id, color=old.color,
            x=new_x, y=new_y, z=new_z,
            rotation=flip_table.get(old.rotation, old.rotation),
            subassembly=dst,
        )
        STATE.parts[inst_id] = inst
        _record(Op("add", inst_id, {"inst": deepcopy(inst)}))
        mirrored += 1
    return {"ok": True, "src": src, "dst": dst, "axis": axis_lower, "parts": mirrored}


CELL_SIZE = 80.0  # LDU. Picked to roughly match a 2x4 brick footprint.


def _cell_keys(aabb: tuple) -> list[tuple[int, int, int]]:
    """Grid cells an AABB occupies. Used to bucket parts for O(n) collision."""
    (xmin, ymin, zmin), (xmax, ymax, zmax) = aabb
    ix0 = int(math.floor(xmin / CELL_SIZE))
    ix1 = int(math.floor((xmax - 1e-6) / CELL_SIZE))
    iy0 = int(math.floor(ymin / CELL_SIZE))
    iy1 = int(math.floor((ymax - 1e-6) / CELL_SIZE))
    iz0 = int(math.floor(zmin / CELL_SIZE))
    iz1 = int(math.floor((zmax - 1e-6) / CELL_SIZE))
    return [(ix, iy, iz)
            for ix in range(ix0, ix1 + 1)
            for iy in range(iy0, iy1 + 1)
            for iz in range(iz0, iz1 + 1)]


_GRID_XZ = 10.0   # LDU. Brick centers land on the half-stud grid by convention.
_GRID_Y = 4.0     # LDU. Bottom face Y lands on quarter-plate grid (baseplate top at -4, plate-tops at multiples of 4 from there).


def _grid_aligned(v: float, grid: float) -> bool:
    """True if v is within 0.1 LDU of a multiple of `grid`."""
    return abs(v - round(v / grid) * grid) < 0.1


def _nearest_grid(v: float, grid: float) -> float:
    return round(v / grid) * grid


@mcp.tool()
def validate_model(max_errors: int = 200, check_support: bool = True) -> dict[str, Any]:
    """Validate the model: collisions, unknown parts, floating, unanchored,
    grid-alignment. Returns a compact 9-field report + structured errors with
    repair suggestions where applicable.

    Args:
        max_errors: cap on the returned error list. Counts still cover everything.
        check_support: include connectivity-based floating + unanchored checks.
    """
    errors: list[dict[str, Any]] = []
    unknown_count = 0
    aabbs: dict[str, tuple] = {}
    for inst in STATE.parts.values():
        part = PART_INDEX.get(inst.part_id)
        if part is None:
            unknown_count += 1
            if len(errors) < max_errors:
                errors.append({"type": "unknown_part", "part_id": inst.part_id,
                               "instance_id": inst.instance_id})
            continue
        aabbs[inst.instance_id] = part_aabb_world(inst, part)

    grid: dict[tuple[int, int, int], list[str]] = {}
    for iid, ab in aabbs.items():
        for key in _cell_keys(ab):
            grid.setdefault(key, []).append(iid)

    checked: set[tuple[str, str]] = set()
    collision_count = 0
    for ids in grid.values():
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                pair = (a, b) if a < b else (b, a)
                if pair in checked:
                    continue
                checked.add(pair)
                if aabbs_overlap(aabbs[a], aabbs[b]):
                    collision_count += 1
                    if len(errors) < max_errors:
                        errors.append({"type": "collision", "instance_a": a,
                                       "instance_b": b,
                                       "note": "AABB-based; corner-touching tiles may falsely overlap."})

    floating_count = 0
    unanchored_count = 0
    grid_errors = 0
    edges_count = 0
    from lego_mcp.connection_graph import (
        find_floating_and_unanchored, vertical_seam_score, wall_bond_quality,
    )
    if check_support:
        graph, edges, anchors, floating_ids, unanchored_ids = find_floating_and_unanchored(STATE.parts)
        edges_count = len(edges)
        floating_count = len(floating_ids)
        unanchored_count = len(unanchored_ids)
        for iid in sorted(floating_ids):
            inst = STATE.parts.get(iid)
            if inst is None:
                continue
            if len(errors) < max_errors:
                errors.append({
                    "type": "floating_part",
                    "instanceId": iid,
                    "message": f"Part {iid} ({inst.part_id}) has no valid connection to the model.",
                    "suggestion": _suggest_for_floating(inst),
                })
        for iid in sorted(unanchored_ids):
            if len(errors) < max_errors:
                errors.append({
                    "type": "unanchored",
                    "instanceId": iid,
                    "message": f"Part {iid} is connected to neighbors but its island doesn't reach the ground.",
                    "suggestion": "Add a connection to a grounded part or extend a wall down to a baseplate.",
                })

    # Grid-alignment check (X/Z to half-stud, Y to quarter-plate).
    for inst in STATE.parts.values():
        if not (_grid_aligned(inst.x, _GRID_XZ) and _grid_aligned(inst.z, _GRID_XZ)
                 and _grid_aligned(inst.y, _GRID_Y)):
            grid_errors += 1
            if len(errors) < max_errors:
                nx = _nearest_grid(inst.x, _GRID_XZ)
                nz = _nearest_grid(inst.z, _GRID_XZ)
                ny = _nearest_grid(inst.y, _GRID_Y)
                errors.append({
                    "type": "invalid_grid_alignment",
                    "instanceId": inst.instance_id,
                    "message": (f"Part {inst.instance_id} at ({inst.x},{inst.y},{inst.z}) "
                                f"is off-grid; nearest grid is ({nx},{ny},{nz})."),
                    "suggestion": (f"Move part {inst.instance_id} by "
                                   f"({nx - inst.x:+g}, {ny - inst.y:+g}, {nz - inst.z:+g}) LDU."),
                })

    seam_score = vertical_seam_score(STATE.parts)
    bond_quality = wall_bond_quality(STATE.parts)

    issue_count = collision_count + unknown_count + floating_count + unanchored_count + grid_errors
    return {
        "valid": issue_count == 0,
        "errors": errors,
        "summary": {
            "parts": len(STATE.parts),
            "connections": edges_count,
            "collisions": collision_count,
            "unknown_parts": unknown_count,
            "floating": floating_count,
            "unanchored": unanchored_count,
            "grid_alignment_errors": grid_errors,
            "vertical_seam_score": seam_score,
            "wall_bond_quality": round(bond_quality, 3),
            "errors_truncated": issue_count > len(errors),
        },
    }


def _suggest_for_floating(inst) -> str:
    """Best-effort repair suggestion."""
    plate_y_options = [-4, -12, -28, -52, -76]
    nearest = min(plate_y_options, key=lambda y: abs(y - inst.y))
    if abs(nearest - inst.y) < 30 and nearest != inst.y:
        return f"Move part {inst.instance_id} from y={inst.y} to y={nearest} (nearest stack-aligned Y)."
    return f"Place a supporting brick directly below part {inst.instance_id}."


@mcp.tool()
def export_ldr(path: str) -> dict[str, Any]:
    """Write the model as a single-file .ldr."""
    Path(path).expanduser().write_text(emit_ldr(STATE))
    return {"ok": True, "path": str(Path(path).expanduser()), "parts": len(STATE.parts)}


@mcp.tool()
def export_mpd(path: str) -> dict[str, Any]:
    """Write the model as a single-block .mpd (multi-block support coming)."""
    Path(path).expanduser().write_text(emit_mpd(STATE))
    return {"ok": True, "path": str(Path(path).expanduser()), "parts": len(STATE.parts)}


@mcp.tool()
def import_ldr(path: str) -> dict[str, Any]:
    """Load .ldr or .mpd. **Replaces the current model.** Clears undo/redo."""
    text = Path(path).expanduser().read_text()
    instances = parse_ldr_text(text)
    _reset_state(Path(path).stem, keep_checkpoints=True)
    for inst in instances:
        inst.instance_id = STATE.new_id()
        STATE.parts[inst.instance_id] = inst
    return {"ok": True, "loaded": len(instances), "model": STATE.name}


@mcp.tool()
def undo() -> dict[str, Any]:
    """Undo the last mutation."""
    if not STATE._undo:
        return {"ok": False, "reason": "nothing to undo"}
    op = STATE._undo.pop()
    _apply_inverse(op)
    STATE._redo.append(op)
    return {"ok": True, "undone": f"{op.kind} {op.instance_id}", "parts": len(STATE.parts)}


@mcp.tool()
def redo() -> dict[str, Any]:
    """Redo the last undone mutation."""
    if not STATE._redo:
        return {"ok": False, "reason": "nothing to redo"}
    op = STATE._redo.pop()
    _apply_forward(op)
    STATE._undo.append(op)
    return {"ok": True, "redone": f"{op.kind} {op.instance_id}", "parts": len(STATE.parts)}


@mcp.tool()
def save_checkpoint(name: str) -> dict[str, Any]:
    """Take a named in-memory snapshot of the current model."""
    STATE._checkpoints[name] = deepcopy(STATE)
    STATE._checkpoints[name]._checkpoints = {}  # avoid recursive memory growth
    STATE._checkpoints[name]._undo = []
    STATE._checkpoints[name]._redo = []
    return {"ok": True, "name": name, "parts": len(STATE.parts)}


@mcp.tool()
def restore_checkpoint(name: str) -> dict[str, Any]:
    """Restore a previously saved checkpoint. Clears undo/redo."""
    snap = STATE._checkpoints.get(name)
    if not snap:
        raise ValueError(f"No checkpoint {name!r}. Saved: {sorted(STATE._checkpoints)}")
    STATE.name = snap.name
    STATE.parts.clear()
    STATE.parts.update(deepcopy(snap.parts))
    STATE._next_id = snap._next_id
    STATE._undo.clear()
    STATE._redo.clear()
    return {"ok": True, "restored": name, "parts": len(STATE.parts)}


@mcp.tool()
def list_checkpoints() -> dict[str, Any]:
    """Names of all checkpoints saved this session."""
    return {"checkpoints": [{"name": n, "parts": len(s.parts)} for n, s in STATE._checkpoints.items()]}


# ---------------------------------------------------------------------------
# Persistent named projects on disk (survive restart).
# ---------------------------------------------------------------------------

PROJECTS_DIR = Path(os.environ.get(
    "LEGO_MCP_PROJECTS",
    str(Path.home() / "Library" / "Application Support" / "lego_mcp" / "projects"),
))


def _project_paths(name: str) -> tuple[Path, Path]:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "untitled"
    base = PROJECTS_DIR / safe
    return base.with_suffix(".mpd"), base.with_suffix(".notes.json")


@mcp.tool()
def save_project(name: str) -> dict[str, Any]:
    """Save the current model (parts + notes) to disk under `name`.

    The model is written as a multi-block MPD; notes go alongside as JSON.
    Survives restart. Use `list_projects()` to see them, `load_project(name)`
    to restore.
    """
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    mpd_path, notes_path = _project_paths(name)
    mpd_path.write_text(emit_mpd(STATE))
    notes_path.write_text(json.dumps({"name": STATE.name, "notes": STATE.notes}))
    return {"ok": True, "name": name, "mpd": str(mpd_path), "notes": str(notes_path),
            "parts": len(STATE.parts)}


@mcp.tool()
def load_project(name: str) -> dict[str, Any]:
    """Replace the current model with a saved project (parts + notes)."""
    mpd_path, notes_path = _project_paths(name)
    if not mpd_path.is_file():
        raise ValueError(f"No project {name!r} at {mpd_path}")
    instances = parse_ldr_text(mpd_path.read_text())
    _reset_state(name)
    for inst in instances:
        inst.instance_id = STATE.new_id()
        STATE.parts[inst.instance_id] = inst
    if notes_path.is_file():
        try:
            data = json.loads(notes_path.read_text())
            STATE.name = data.get("name", name)
            STATE.notes.update(data.get("notes", {}))
        except (OSError, ValueError):
            pass
    return {"ok": True, "name": name, "parts": len(STATE.parts),
            "notes": len(STATE.notes)}


@mcp.tool()
def list_projects() -> dict[str, Any]:
    """List every saved project on disk."""
    if not PROJECTS_DIR.is_dir():
        return {"projects": []}
    projects = []
    for f in sorted(PROJECTS_DIR.glob("*.mpd")):
        projects.append({"name": f.stem, "path": str(f),
                         "size_bytes": f.stat().st_size})
    return {"projects": projects, "dir": str(PROJECTS_DIR)}


# ---------------------------------------------------------------------------
# Reference notes (sticky observations for multimodal workflows: a user
# uploads architectural plans, Claude estimates "left tower is 12 studs at
# base, narrows to 6", and saves a note. Notes persist with the project.)
# ---------------------------------------------------------------------------

@mcp.tool()
def add_note(key: str, text: str) -> dict[str, Any]:
    """Record a free-text observation under `key`. Overwrites if key exists.

    Useful for tracking dimensions/proportions/decisions across many turns
    when building from reference imagery.
    """
    STATE.notes[key] = text
    return {"ok": True, "key": key, "total_notes": len(STATE.notes)}


@mcp.tool()
def get_note(key: str) -> dict[str, Any]:
    """Read a previously-saved note."""
    if key not in STATE.notes:
        raise ValueError(f"No note {key!r}. Saved: {sorted(STATE.notes)}")
    return {"key": key, "text": STATE.notes[key]}


@mcp.tool()
def list_notes() -> dict[str, Any]:
    """All saved notes for the current model."""
    return {"notes": [{"key": k, "text": v} for k, v in STATE.notes.items()]}


@mcp.tool()
def remove_note(key: str) -> dict[str, Any]:
    """Delete a note."""
    STATE.notes.pop(key, None)
    return {"ok": True, "key": key, "remaining": len(STATE.notes)}


# render_model is registered when render.py imports cleanly (Pillow present).
try:
    from lego_mcp.render import render_model_png  # noqa: F401

    @mcp.tool()
    def render_model(width: int = 800, height: int = 600) -> dict[str, Any]:
        """Render the model as an isometric PNG.

        Writes to ./renders/<timestamp>_<model>.png so the history is preserved —
        come back later and scroll the renders folder to see how the model evolved.
        Also updates ./renders/latest.png as a convenience pointer.
        """
        from datetime import datetime

        renders_dir = Path("renders").resolve()
        renders_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in STATE.name) or "model"
        out = renders_dir / f"{stamp}_{safe_name}.png"
        png = render_model_png(STATE.parts, PART_INDEX, width=width, height=height)
        out.write_bytes(png)
        # Convenience latest pointer (real file, not symlink — works on all filesystems).
        (renders_dir / "latest.png").write_bytes(png)
        return {"ok": True, "path": str(out), "latest": str(renders_dir / "latest.png"),
                "parts": len(STATE.parts), "width": width, "height": height}
except ImportError:
    log.info("Pillow not available; render_model tool disabled.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """Load the part library (built-in or installed), then serve over stdio."""
    _ensure_library_loaded()
    from lego_mcp.prompts import register_prompts, register_resources
    from lego_mcp.helpers import register_helpers
    register_prompts(mcp)
    register_resources(mcp)
    register_helpers(mcp)
    log.info("LegoMCP starting. %d parts loaded.", len(PART_INDEX))
    mcp.run()
