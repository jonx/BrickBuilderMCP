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
    # When importing models authored elsewhere, sub-file references can carry
    # rotation matrices that aren't one of the 6 canonical rotations (small
    # tilts, full 3D rotations). We preserve the raw 9-float row-major matrix
    # here so render/export stay lossless. When set, `matrix` is the truth
    # and `rotation` is a placeholder ("identity"). When None, `rotation` is
    # authoritative — the normal authored-by-our-tools flow.
    matrix: tuple[float, float, float, float, float, float, float, float, float] | None = None


def effective_matrix(inst: PartInstance) -> Matrix:
    """Return the rotation matrix the renderer/exporter should use for `inst`.

    `inst.matrix` (raw 9-float) takes precedence if set; otherwise fall back
    to the named canonical rotation via `resolve_rotation`.
    """
    if inst.matrix is not None:
        return inst.matrix  # type: ignore[return-value]
    return resolve_rotation(inst.rotation)


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
    # Builder mode: `built` is the set of instance_ids physically placed so
    # far. In design mode (default), add_part auto-adds the new id to `built`
    # so legacy behavior is preserved. After `start_builder_session()` the
    # set is cleared and the LLM advances by calling mark_built(...).
    built: set[str] = field(default_factory=set)
    builder_mode: bool = False
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
    try:
        _maybe_autosave()
    except Exception:  # noqa: BLE001 — autosave must never break the user's flow
        pass


def _apply_forward(op: Op) -> None:
    if op.kind == "add":
        STATE.parts[op.instance_id] = deepcopy(op.data["inst"])
        if not STATE.builder_mode:
            STATE.built.add(op.instance_id)
    elif op.kind == "remove":
        STATE.parts.pop(op.instance_id, None)
        STATE.built.discard(op.instance_id)
    elif op.kind == "move":
        inst = STATE.parts[op.instance_id]
        inst.x, inst.y, inst.z = op.data["new_pos"]
    elif op.kind == "rotate":
        STATE.parts[op.instance_id].rotation = op.data["new_rot"]


def _apply_inverse(op: Op) -> None:
    if op.kind == "add":
        STATE.parts.pop(op.instance_id, None)
        STATE.built.discard(op.instance_id)
    elif op.kind == "remove":
        STATE.parts[op.instance_id] = deepcopy(op.data["inst"])
        if not STATE.builder_mode:
            STATE.built.add(op.instance_id)
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
    m = effective_matrix(inst)
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
    r"(.+\.(?:dat|ldr|mpd))\s*$",
    re.IGNORECASE,
)


def _matrix_to_rotation_name(m: Matrix) -> str | None:
    """Reverse-lookup a canonical rotation name for a 9-float matrix.

    Returns the name if it matches one of the 6 canonical rotations within
    tolerance; otherwise returns None so the caller can preserve the raw
    matrix on the PartInstance.
    """
    for name, ref in ROTATIONS.items():
        if all(abs(a - b) < 1e-3 for a, b in zip(m, ref)):
            return name
    return None


def _emit_inst_line(inst: PartInstance) -> str:
    m = effective_matrix(inst)
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
            world_mat = world[3:12]
            rot_name = _matrix_to_rotation_name(world_mat)  # type: ignore[arg-type]
            inst = PartInstance(
                instance_id="0",  # caller will assign
                part_id=part_stem,
                color=color,
                x=world[0], y=world[1], z=world[2],
                rotation=rot_name or "identity",
                subassembly=name,
            )
            if rot_name is None:
                # Non-canonical rotation — preserve the raw matrix verbatim.
                inst.matrix = world_mat  # type: ignore[assignment]
            results.append(inst)
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
    if d.get("matrix") is None:
        d.pop("matrix", None)
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
    STATE.built.clear()
    STATE.builder_mode = False
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
    # Design mode (default): every add is "placed". Builder mode: the new part
    # is part of the target but not yet built — the LLM advances via mark_built.
    if not STATE.builder_mode:
        STATE.built.add(inst_id)
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


def _orientation_hint(p) -> dict[str, Any]:
    """For slopes (and any asymmetric part), derive a human-readable hint
    describing where the 'high' (stud-bearing) edge sits in identity rotation.

    Heuristic:
    - The stud row centroid vs the bbox centroid gives a direction.
    - Whichever axis (X or Z) the offset is larger on, that's the high-edge axis.
    - Sign of the offset gives +/-.
    """
    name_lower = p.name.lower()
    is_slope = "slope" in name_lower
    if not p.studs:
        return {"shape": "tile_or_smooth_top" if "tile" in name_lower else "no_top_studs"}
    if not is_slope:
        return {"shape": "cuboid", "stud_count": len(p.studs)}
    # Slope: locate stud centroid vs bbox centroid
    (minx, miny, minz), (maxx, maxy, maxz) = p.bbox
    cx = (minx + maxx) / 2
    cz = (minz + maxz) / 2
    sx = sum(s[0] for s in p.studs) / len(p.studs)
    sz = sum(s[2] for s in p.studs) / len(p.studs)
    dx, dz = sx - cx, sz - cz
    # Pick the dominant axis
    if abs(dx) >= abs(dz):
        side = "+X" if dx > 0 else "-X"
        low = "-X" if dx > 0 else "+X"
    else:
        side = "+Z" if dz > 0 else "-Z"
        low = "-Z" if dz > 0 else "+Z"
    summary = (f"Slope in identity rotation: HIGH edge (with studs) is at {side} side; "
               f"slope descends toward {low}. To flip the slope, use rot180y. "
               f"rot90y rotates the high edge to {'+Z' if side == '+X' else '-Z' if side == '-X' else '-X' if side == '+Z' else '+X'} side.")
    return {"shape": "slope", "high_edge": side, "low_edge": low,
            "stud_count": len(p.studs), "summary": summary}


@mcp.tool()
def get_part_info(part_id: str) -> dict[str, Any]:
    """Get dimensions, stud info, and (for slopes) orientation hint for a part_id.

    For slopes the response includes an `orientation` block telling you which
    side has the un-sloped high edge in identity rotation — so you don't have
    to guess after rotation.
    """
    p = _require_part(part_id)
    return {"part_id": p.part_id, "name": p.name,
            "width_ldu": p.width, "depth_ldu": p.depth, "height_ldu": p.height,
            "width_studs": round(p.width / 20, 2),
            "depth_studs": round(p.depth / 20, 2),
            "height_plates": round(p.height / 8, 2),
            "stud_count": len(p.studs),
            "orientation": _orientation_hint(p),
            "bbox_local": {"min": list(p.bbox[0]), "max": list(p.bbox[1])}}


@mcp.tool()
def list_colors() -> dict[str, Any]:
    """List supported color names and their LDraw IDs."""
    return {"colors": [{"name": name, "id": cid} for name, (cid, _) in COLORS.items()]}


@mcp.tool()
def parts_that_mount_on(part_id: str, limit: int = 20,
                         min_studs_matched: int = 1,
                         exclude_non_structural: bool = True) -> dict[str, Any]:
    """Reverse search: given a part ID, return up to `limit` other parts that
    can sit on top of it with at least `min_studs_matched` stud-receiver matings.
    By default, sticker/print/decorative catalog entries are excluded.

    Indexed across the full catalog (~23k parts); first call builds the index
    (~1s), subsequent calls return in milliseconds. Use this to find wheels
    that mate to an axle plate, plates that fit a brick, tiles that cap a
    surface, etc.
    """
    from lego_mcp.mount_index import parts_that_mount_on as _mount
    _ensure_library_loaded()
    target = _require_part(part_id)
    excludes = (
        "sticker", "decal", "pattern", "print", "sheet", "cardboard",
        "catalog", "box", "poster",
    ) if exclude_non_structural else ()
    results = _mount(target, PART_INDEX, limit=limit,
                      min_studs_matched=min_studs_matched,
                      exclude_keywords=excludes)
    return {"target": part_id, "target_name": target.name.strip(),
            "exclude_non_structural": exclude_non_structural,
            "result_count": len(results),
            "results": results}


@mcp.tool()
def find_connections(part_a_id: str, part_b_id: str,
                      full_nesting_only: bool = False,
                      min_studs_matched: int = 1) -> dict[str, Any]:
    """Enumerate every valid LEGO connection between two parts.

    Returns the list of relative placements where B stacks on A (and vice
    versa). Increase `min_studs_matched` to reduce noisy partial placements.
    Set full_nesting_only=True to keep only placements where ALL of B's
    receptors mate (i.e. B's full footprint sits inside A's stud area).
    """
    from lego_mcp.connections import find_connections as _find
    a = _require_part(part_a_id)
    b = _require_part(part_b_id)
    return _find(a, b, full_nesting_only=full_nesting_only,
                 min_studs_matched=min_studs_matched)


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


def _parts_for_subassembly(name: str | None) -> dict[str, PartInstance]:
    """Return a part dict filtered by subassembly. None means all parts."""
    if name is None or name == "":
        return dict(STATE.parts)
    selected = {iid: p for iid, p in STATE.parts.items() if p.subassembly == name}
    if not selected:
        raise ValueError(f"subassembly {name!r} has no parts")
    return selected


@mcp.tool()
def move_subassembly(name: str, dx: float = 0, dy: float = 0, dz: float = 0) -> dict[str, Any]:
    """Move every part tagged with `name` by an offset. Undoable per part.

    Useful after `find_subassembly_connections` suggests an offset for joining
    one rigid module to another.
    """
    selected = _parts_for_subassembly(name)
    moved = 0
    for inst in selected.values():
        old_pos = (inst.x, inst.y, inst.z)
        new_pos = (inst.x + dx, inst.y + dy, inst.z + dz)
        inst.x, inst.y, inst.z = new_pos
        _record(Op("move", inst.instance_id, {"old_pos": old_pos, "new_pos": new_pos}))
        moved += 1
    return {"ok": True, "subassembly": name, "moved": moved,
            "offset": [dx, dy, dz]}


@mcp.tool()
def analyze_assembly_ports(subassembly: str | None = None,
                           max_connectors: int = 0,
                           max_ports: int = 50) -> dict[str, Any]:
    """List exposed studs/receivers and clustered attachment ports.

    Args:
        subassembly: Optional subassembly name. Omit for the whole model.
        max_connectors: Cap detailed connector rows; counts still include all.
            Defaults to 0 so normal responses stay port-focused.
        max_ports: Cap returned port clusters; largest ports are returned first.
    """
    from lego_mcp.assembly_ports import analyze_ports
    selected = _parts_for_subassembly(subassembly)
    result = analyze_ports(selected, max_connectors=max_connectors, max_ports=max_ports)
    result["subassembly"] = subassembly
    return result


@mcp.tool()
def find_subassembly_connections(movable: str,
                                 target: str | None = None,
                                 limit: int = 20) -> dict[str, Any]:
    """Find offsets that would connect one subassembly to another assembly.

    Args:
        movable: The subassembly you intend to move.
        target: Target subassembly. Omit to use every part not in `movable`.
        limit: Number of candidate offsets to return, sorted by matched studs.
    """
    from lego_mcp.assembly_ports import connection_offsets
    movable_parts = _parts_for_subassembly(movable)
    if target:
        target_parts = _parts_for_subassembly(target)
    else:
        target_parts = {
            iid: p for iid, p in STATE.parts.items()
            if p.subassembly != movable
        }
        if not target_parts:
            raise ValueError("target omitted but no other parts are available")
    candidates = connection_offsets(movable_parts, target_parts, limit=limit)
    return {
        "movable": movable,
        "target": target,
        "candidates": candidates,
        "count": len(candidates),
    }


@mcp.tool()
def plan_build_sequence(subassembly: str | None = None,
                        max_steps: int = 50,
                        start_after: int = 0) -> dict[str, Any]:
    """Return human-style instructions for building the current model.

    The sequence is ordered so every returned step is either on the ground or
    supported by parts from earlier steps. Use `start_after`/`max_steps` for
    paged instructions on large models.
    """
    from lego_mcp.build_steps import plan_build_sequence as _plan
    selected = _parts_for_subassembly(subassembly)
    result = _plan(selected, PART_INDEX, part_aabb_world,
                   max_steps=max_steps, start_after=start_after)
    result["subassembly"] = subassembly
    return result


@mcp.tool()
def next_build_step(subassembly: str | None = None,
                    built_count: int = 0) -> dict[str, Any]:
    """Return the next physically placeable part after `built_count` steps.

    This is a small convenience wrapper over `plan_build_sequence`; a client
    can keep advancing `built_count` to walk the model piece by piece.
    """
    if built_count < 0:
        raise ValueError("built_count must be >= 0")
    planned = plan_build_sequence(subassembly=subassembly,
                                  max_steps=1,
                                  start_after=built_count)
    steps = planned.get("steps", [])
    return {
        "ok": planned["ok"] and bool(steps),
        "subassembly": subassembly,
        "built_count": built_count,
        "total_parts": planned.get("total_parts", 0),
        "next": steps[0] if steps else None,
        "complete": planned["ok"] and built_count >= planned.get("total_parts", 0),
        "blocked_count": planned.get("blocked_count", 0),
        "blocked": planned.get("blocked", []),
    }


# ---------------------------------------------------------------------------
# Builder mode: partial → target workflow.
# `STATE.built` is the set of instance_ids that have been physically placed.
# In design mode (default), every add_part auto-adds to `built`. After
# start_builder_session() the model becomes a TARGET and the LLM advances by
# calling mark_built(...).
# ---------------------------------------------------------------------------

@mcp.tool()
def start_builder_session() -> dict[str, Any]:
    """Treat the current model as a build TARGET. Clears the 'built' set so
    every part becomes unbuilt; subsequent add_part calls won't auto-mark as
    built either. Use this when you have a complete target model and want to
    walk through placing it one part at a time."""
    STATE.builder_mode = True
    target_total = len(STATE.parts)
    STATE.built.clear()
    return {"ok": True, "builder_mode": True, "target_parts": target_total,
            "built": 0}


@mcp.tool()
def end_builder_session(mark_all_built: bool = True) -> dict[str, Any]:
    """Exit builder mode. By default marks all parts as built (so subsequent
    operations see a complete model). Pass mark_all_built=False to leave the
    built set as-is."""
    STATE.builder_mode = False
    if mark_all_built:
        STATE.built = set(STATE.parts.keys())
    return {"ok": True, "builder_mode": False, "built": len(STATE.built)}


@mcp.tool()
def mark_built(instance_id: str) -> dict[str, Any]:
    """Mark one part as physically placed. Validates that the part is a real
    target part (i.e. exists in STATE.parts)."""
    if instance_id not in STATE.parts:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    STATE.built.add(instance_id)
    return {"ok": True, "instance_id": instance_id,
            "built": len(STATE.built), "total": len(STATE.parts)}


@mcp.tool()
def mark_built_batch(instance_ids: list[str]) -> dict[str, Any]:
    """Mark multiple parts as built in one call. Skips ids that don't exist
    in the target model (returns them as `unknown`)."""
    unknown: list[str] = []
    placed = 0
    for iid in instance_ids:
        if iid not in STATE.parts:
            unknown.append(iid)
            continue
        if iid not in STATE.built:
            STATE.built.add(iid)
            placed += 1
    return {"ok": True, "newly_built": placed, "unknown": unknown,
            "built": len(STATE.built), "total": len(STATE.parts)}


@mcp.tool()
def unmark_built(instance_id: str) -> dict[str, Any]:
    """Reverse a mark_built — moves the part back to the unbuilt set."""
    STATE.built.discard(instance_id)
    return {"ok": True, "instance_id": instance_id, "built": len(STATE.built)}


@mcp.tool()
def reset_build_progress() -> dict[str, Any]:
    """Clear the entire built set (start over)."""
    STATE.built.clear()
    return {"ok": True, "built": 0, "total": len(STATE.parts)}


@mcp.tool()
def builder_status() -> dict[str, Any]:
    """One-shot snapshot of building progress.

    Returns:
        total: target part count
        built: how many have been placed
        remaining: target - built
        next_up: the next placeable unbuilt part (or None if blocked/complete)
        blocked_count: unbuilt parts that can't be placed yet (waiting on supporters)
        complete: True if every target part is built
    """
    from lego_mcp.build_steps import next_unbuilt_step as _next
    r = _next(STATE.parts, PART_INDEX, part_aabb_world, STATE.built, limit=1)
    next_up = r["candidates"][0] if r["candidates"] else None
    return {
        "builder_mode": STATE.builder_mode,
        "total": r["total_parts"],
        "built": r["built_count"],
        "remaining": r["remaining"],
        "next_up": next_up,
        "blocked_count": r["blocked_count"],
        "complete": r["complete"],
    }


@mcp.tool()
def next_unbuilt_step(limit: int = 1) -> dict[str, Any]:
    """Return the next `limit` placeable unbuilt parts, considering only
    currently-built parts as available supporters. Use this in a builder loop:
    call this, place the brick (or call mark_built on the returned id), repeat.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    from lego_mcp.build_steps import next_unbuilt_step as _next
    return _next(STATE.parts, PART_INDEX, part_aabb_world, STATE.built, limit=limit)


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


# ---------------------------------------------------------------------------
# Debug / inspection helpers (used by render_validation, inspect_part,
# collision_detail, describe_errors).
# ---------------------------------------------------------------------------

def _validation_status_sets() -> dict[str, Any]:
    """Categorize every part by its validation status. Returns sets keyed by
    status name plus a `collision_pairs` list for collision_detail.

    Single source of truth so render_validation and inspect_part stay
    consistent with validate_model.
    """
    from lego_mcp.connection_graph import find_floating_and_unanchored
    parts = STATE.parts
    aabbs: dict[str, tuple] = {}
    unknown: set[str] = set()
    for iid, inst in parts.items():
        p = PART_INDEX.get(inst.part_id)
        if p is None:
            unknown.add(iid)
            continue
        aabbs[iid] = part_aabb_world(inst, p)

    # Collisions via the same grid as validate_model
    grid: dict[tuple[int, int, int], list[str]] = {}
    for iid, ab in aabbs.items():
        for key in _cell_keys(ab):
            grid.setdefault(key, []).append(iid)
    collisions: set[str] = set()
    collision_pairs: list[tuple[str, str]] = []
    checked: set[tuple[str, str]] = set()
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
                    collisions.add(a)
                    collisions.add(b)
                    collision_pairs.append(pair)

    _, _, _, floating, unanchored = find_floating_and_unanchored(parts)

    grid_errors: set[str] = set()
    for inst in parts.values():
        if not (_grid_aligned(inst.x, _GRID_XZ) and _grid_aligned(inst.z, _GRID_XZ)
                 and _grid_aligned(inst.y, _GRID_Y)):
            grid_errors.add(inst.instance_id)

    ok = set(parts.keys()) - collisions - floating - unanchored - grid_errors - unknown
    return {
        "ok": ok,
        "collisions": collisions,
        "floating": floating,
        "unanchored": unanchored,
        "grid_errors": grid_errors,
        "unknown": unknown,
        "collision_pairs": collision_pairs,
        "aabbs": aabbs,
    }


def _resolve_export_path(path: str) -> Path:
    """Honor absolute paths and ~-paths verbatim. Relative paths land in the
    user-writable renders dir so exports work under Claude Desktop where the
    cwd is read-only."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = _renders_dir() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@mcp.tool()
def export_ldr(path: str) -> dict[str, Any]:
    """Write the model as a single-file .ldr. Relative paths land in the
    server's renders dir so exports work under Claude Desktop."""
    out = _resolve_export_path(path)
    out.write_text(emit_ldr(STATE))
    return {"ok": True, "path": str(out), "parts": len(STATE.parts)}


@mcp.tool()
def export_mpd(path: str) -> dict[str, Any]:
    """Write the model as a multi-block .mpd. Relative paths land in the
    server's renders dir so exports work under Claude Desktop."""
    out = _resolve_export_path(path)
    out.write_text(emit_mpd(STATE))
    return {"ok": True, "path": str(out), "parts": len(STATE.parts)}


@mcp.tool()
def import_ldr(path: str) -> dict[str, Any]:
    """Load .ldr or .mpd. **Replaces the current model.** Clears undo/redo."""
    _ensure_library_loaded()
    text = Path(path).expanduser().read_text()
    instances = parse_ldr_text(text)
    _reset_state(Path(path).stem, keep_checkpoints=True)
    for inst in instances:
        inst.instance_id = STATE.new_id()
        STATE.parts[inst.instance_id] = inst
    known = sum(1 for i in instances if i.part_id in PART_INDEX)
    return {
        "ok": True,
        "loaded": len(instances),
        "known_parts": known,
        "unknown_parts": len(instances) - known,
        "model": STATE.name,
    }


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

AUTOSAVE_NAME = "_autosave"
AUTOSAVE_EVERY_N_MUTATIONS = 25


def _project_paths(name: str) -> tuple[Path, Path]:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "untitled"
    base = PROJECTS_DIR / safe
    return base.with_suffix(".mpd"), base.with_suffix(".notes.json")


def _maybe_autosave() -> None:
    """Called after each mutation. Saves to _autosave every N mutations.
    Atomic-ish: writes to .tmp then renames so a crash mid-write doesn't
    leave a corrupt autosave."""
    n = len(STATE._undo)
    if n == 0 or n % AUTOSAVE_EVERY_N_MUTATIONS != 0:
        return
    try:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        mpd_path, notes_path = _project_paths(AUTOSAVE_NAME)
        tmp_mpd = mpd_path.with_suffix(".mpd.tmp")
        tmp_notes = notes_path.with_suffix(".notes.json.tmp")
        tmp_mpd.write_text(emit_mpd(STATE))
        tmp_notes.write_text(json.dumps({"name": STATE.name, "notes": STATE.notes,
                                          "mutations": n}))
        tmp_mpd.replace(mpd_path)
        tmp_notes.replace(notes_path)
    except OSError as e:  # never let autosave break a build
        log.warning("autosave failed: %s", e)


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
        if f.stem.startswith("_"):
            continue
        projects.append({"name": f.stem, "path": str(f),
                         "size_bytes": f.stat().st_size})
    return {"projects": projects, "dir": str(PROJECTS_DIR)}


@mcp.tool()
def restore_autosave() -> dict[str, Any]:
    """Load the most recent autosave snapshot (written every 25 mutations).
    Useful when the server restarted mid-build and you want to pick up where
    you left off."""
    mpd_path, _ = _project_paths(AUTOSAVE_NAME)
    if not mpd_path.is_file():
        return {"ok": False, "reason": "no autosave found", "path": str(mpd_path)}
    return load_project(AUTOSAVE_NAME)


@mcp.tool()
def autosave_status() -> dict[str, Any]:
    """Where the autosave lives and when it was last written, plus the
    current mutation count toward the next autosave."""
    mpd_path, _ = _project_paths(AUTOSAVE_NAME)
    exists = mpd_path.is_file()
    return {
        "autosave_path": str(mpd_path),
        "exists": exists,
        "size_bytes": mpd_path.stat().st_size if exists else 0,
        "mutations_so_far": len(STATE._undo),
        "save_every_n": AUTOSAVE_EVERY_N_MUTATIONS,
        "next_save_in": AUTOSAVE_EVERY_N_MUTATIONS - (len(STATE._undo) % AUTOSAVE_EVERY_N_MUTATIONS),
    }


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


def _renders_dir() -> Path:
    """Where renders go. Defaults to a user-writable absolute path so the
    server works under Claude Desktop / launchd / any GUI host where the
    working directory isn't user-writable. Override with LEGO_MCP_RENDERS_DIR.
    """
    override = os.environ.get("LEGO_MCP_RENDERS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "lego_mcp" / "renders").resolve()


# render_model is registered when render.py imports cleanly (Pillow present).
try:
    from lego_mcp.render import render_model_png  # noqa: F401
    from mcp.server.fastmcp import Image as MCPImage

    def _render_to_disk_and_image(png: bytes, name_suffix: str = "") -> tuple[dict[str, Any], MCPImage]:
        """Common tail: write PNG + latest.png, return (summary_dict, MCPImage)."""
        from datetime import datetime
        renders_dir = _renders_dir()
        renders_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in STATE.name) or "model"
        out = renders_dir / f"{stamp}_{safe_name}{name_suffix}.png"
        out.write_bytes(png)
        (renders_dir / "latest.png").write_bytes(png)
        summary = {"ok": True, "path": str(out),
                   "latest": str(renders_dir / "latest.png"),
                   "renders_dir": str(renders_dir),
                   "bytes": len(png)}
        return summary, MCPImage(data=png, format="png")

    def _inline_mode() -> str:
        """data_uri (default, works in any chat client) / file_url (tiny, only
        works where the client renders local file links — Claude Desktop has
        been observed to) / none (skip the markdown text entirely, just rely
        on the MCPImage block).

        Read fresh on every render so the user can flip modes mid-session via
        `export LEGO_MCP_INLINE_MODE=file_url` and a tool restart.
        """
        return os.environ.get("LEGO_MCP_INLINE_MODE", "data_uri").strip().lower()

    def _markdown_image(png: bytes, file_path: str, alt: str = "render") -> str | None:
        """Markdown text-block image. Mode-dependent (see _inline_mode)."""
        mode = _inline_mode()
        if mode == "none":
            return None
        if mode == "file_url":
            # file:// scheme. Path must be absolute (it always is here — we
            # write under the resolved renders dir).
            return f"![{alt}](file://{file_path})"
        # default: data_uri
        import base64
        b64 = base64.b64encode(png).decode("ascii")
        return f"![{alt}](data:image/png;base64,{b64})"

    def _render_response(png: bytes, summary: dict[str, Any], alt: str = "render") -> list:
        """Standard response: [markdown-inline-image?, summary_dict, MCPImage].

        The MCPImage block is ALWAYS present — that's what the LLM's vision
        channel sees. The leading markdown text is for the human's chat-UI
        inline preview and may be omitted (mode='none') or use a file:// URL
        to save bytes (mode='file_url').
        """
        blocks: list = []
        md = _markdown_image(png, summary.get("path", ""), alt)
        if md is not None:
            blocks.append(md)
        blocks.append(summary)
        blocks.append(MCPImage(data=png, format="png"))
        return blocks

    @mcp.tool()
    def render_model(width: int = 800, height: int = 600,
                     color_mode: str = "model",
                     hidden_edges: bool = False) -> list:
        """Render the model as an isometric PNG. Returns BOTH the image (inline
        for the LLM to see) AND a summary dict with the disk path.

        Disk: <renders_dir>/<timestamp>_<model>.png + latest.png.
        Override the directory with LEGO_MCP_RENDERS_DIR env var.

        Args:
            color_mode: "model" uses actual part colors. "instance" assigns a
                different color to each piece. "row" colors each brick course.
                "rotation" colors by orientation.
            hidden_edges: draw fully covered/internal contact faces as dotted
                guide lines. Defaults false for a cleaner inspection render.
        """
        _ensure_library_loaded()
        png = render_model_png(STATE.parts, PART_INDEX, width=width, height=height,
                               color_mode=color_mode, hidden_edges=hidden_edges)
        summary, _img = _render_to_disk_and_image(png)
        summary.update({"parts": len(STATE.parts), "width": width, "height": height,
                        "color_mode": color_mode, "hidden_edges": hidden_edges})
        return _render_response(png, summary, alt="model")

    @mcp.tool()
    def render_progress(width: int = 800, height: int = 600,
                         color_mode: str = "model",
                         hidden_edges: bool = False) -> list:
        """Render the build-in-progress: parts in STATE.built render normally,
        unbuilt parts show as ghosts (washed-out, no studs). Returns BOTH the
        image (inline for the LLM) AND a summary dict with built/total counts.
        """
        _ensure_library_loaded()
        png = render_model_png(STATE.parts, PART_INDEX, width=width, height=height,
                               color_mode=color_mode, hidden_edges=hidden_edges,
                               built_set=STATE.built)
        summary, _img = _render_to_disk_and_image(png, name_suffix="_progress")
        summary.update({"built": len(STATE.built), "total": len(STATE.parts),
                        "width": width, "height": height})
        return _render_response(png, summary, alt="progress")

    @mcp.tool()
    def view_latest_render() -> list:
        """Return the most recent render as an inline image. Useful if you've
        navigated away from a render call or want to see the current state
        without re-rendering."""
        latest = _renders_dir() / "latest.png"
        if not latest.is_file():
            return [{"ok": False, "reason": "no render yet — call render_model first"}]
        png = latest.read_bytes()
        return _render_response(png,
                                 {"ok": True, "path": str(latest), "bytes": len(png)},
                                 alt="latest")

    @mcp.tool()
    def render_validation(width: int = 900, height: int = 700) -> list:
        """Render with parts color-coded by validation status:
        - GREEN: ok
        - RED:    collision
        - ORANGE: floating (no support)
        - PURPLE: unanchored (connected island that doesn't reach ground)
        - YELLOW: off the stud grid
        - GRAY:   unknown part_id

        Single render shows you exactly where the problems are spatially —
        much faster than mapping ID lists from validate_model() onto positions.
        Returns [summary_dict, image] like render_model.
        """
        _ensure_library_loaded()
        status = _validation_status_sets()
        STATUS_COLORS = {
            "ok":          (140, 200, 140),  # green
            "collision":   (220,  60,  60),  # red
            "floating":    (245, 165,  55),  # orange
            "unanchored":  (170,  90, 200),  # purple
            "grid_error":  (240, 220,  60),  # yellow
            "unknown":     (130, 130, 130),  # gray
        }
        override: dict[str, tuple[int, int, int]] = {}
        # Precedence: collision > floating > unanchored > grid_error > unknown > ok.
        # (A part can be in multiple sets — show the most actionable one.)
        for iid in STATE.parts:
            if iid in status["collisions"]:
                override[iid] = STATUS_COLORS["collision"]
            elif iid in status["floating"]:
                override[iid] = STATUS_COLORS["floating"]
            elif iid in status["unanchored"]:
                override[iid] = STATUS_COLORS["unanchored"]
            elif iid in status["grid_errors"]:
                override[iid] = STATUS_COLORS["grid_error"]
            elif iid in status["unknown"]:
                override[iid] = STATUS_COLORS["unknown"]
            else:
                override[iid] = STATUS_COLORS["ok"]
        png = render_model_png(STATE.parts, PART_INDEX, width=width, height=height,
                               hidden_edges=False,
                               instance_color_override=override)
        summary, _img = _render_to_disk_and_image(png, name_suffix="_validation")
        summary.update({
            "parts": len(STATE.parts),
            "ok": len(status["ok"]),
            "collisions": len(status["collisions"]),
            "floating": len(status["floating"]),
            "unanchored": len(status["unanchored"]),
            "grid_errors": len(status["grid_errors"]),
            "unknown": len(status["unknown"]),
            "legend": {k: f"rgb{v}" for k, v in STATUS_COLORS.items()},
        })
        return _render_response(png, summary, alt="validation")
except ImportError:
    log.info("Pillow not available; render_model tool disabled.")


# ---------------------------------------------------------------------------
# Debug / inspection tools (work without Pillow).
# ---------------------------------------------------------------------------

def _aabb_intersection(a: tuple, b: tuple) -> tuple | None:
    """AABB intersection in 3D. Returns ((minx,miny,minz),(maxx,maxy,maxz))
    or None if no overlap."""
    (amin, amax), (bmin, bmax) = a, b
    lo = (max(amin[0], bmin[0]), max(amin[1], bmin[1]), max(amin[2], bmin[2]))
    hi = (min(amax[0], bmax[0]), min(amax[1], bmax[1]), min(amax[2], bmax[2]))
    if lo[0] >= hi[0] or lo[1] >= hi[1] or lo[2] >= hi[2]:
        return None
    return (lo, hi)


def _smallest_separation(a: tuple, b: tuple) -> dict[str, float]:
    """For two overlapping AABBs, the smallest translation along each axis
    that would clear the overlap. The LLM picks the cheapest."""
    inter = _aabb_intersection(a, b)
    if inter is None:
        return {}
    (ilo, ihi) = inter
    dx = ihi[0] - ilo[0]
    dy = ihi[1] - ilo[1]
    dz = ihi[2] - ilo[2]
    # Direction: positive moves B away from A if B's center is at +x relative to A's, etc.
    (amin, amax), (bmin, bmax) = a, b
    a_cx, b_cx = (amin[0] + amax[0]) / 2, (bmin[0] + bmax[0]) / 2
    a_cz, b_cz = (amin[2] + amax[2]) / 2, (bmin[2] + bmax[2]) / 2
    a_cy, b_cy = (amin[1] + amax[1]) / 2, (bmin[1] + bmax[1]) / 2
    return {
        "move_x": dx if b_cx >= a_cx else -dx,
        "move_y": dy if b_cy >= a_cy else -dy,
        "move_z": dz if b_cz >= a_cz else -dz,
    }


@mcp.tool()
def inspect_part(instance_id: str, neighbor_studs: int = 2) -> dict[str, Any]:
    """Focused diagnostic for ONE part: position, AABB, neighbors,
    supporters/supported, collisions, and validation status flags. Cuts
    debugging from 'find ID in list_parts dump' to one call.

    `neighbor_studs`: include parts within this many studs in XZ (default 2).
    """
    inst = STATE.parts.get(instance_id)
    if inst is None:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    p = PART_INDEX.get(inst.part_id)
    aabb = part_aabb_world(inst, p) if p else None
    status = _validation_status_sets()
    flags = []
    if instance_id in status["collisions"]: flags.append("collision")
    if instance_id in status["floating"]: flags.append("floating")
    if instance_id in status["unanchored"]: flags.append("unanchored")
    if instance_id in status["grid_errors"]: flags.append("grid_misalignment")
    if instance_id in status["unknown"]: flags.append("unknown_part")
    if not flags: flags.append("ok")

    # Collisions this part is involved in
    collides_with: list[dict[str, Any]] = []
    for (a, b) in status["collision_pairs"]:
        if a == instance_id or b == instance_id:
            other = b if a == instance_id else a
            other_inst = STATE.parts.get(other)
            other_aabb = status["aabbs"].get(other)
            if other_inst is None or other_aabb is None or aabb is None:
                continue
            inter = _aabb_intersection(aabb, other_aabb)
            sep = _smallest_separation(aabb, other_aabb)
            collides_with.append({
                "other": other, "other_part_id": other_inst.part_id,
                "other_position": [other_inst.x, other_inst.y, other_inst.z],
                "overlap_region": ({"min": list(inter[0]), "max": list(inter[1])}
                                   if inter else None),
                "smallest_separation_ldu": sep,
            })

    # Neighbors via the connection graph (parts mating with this one)
    from lego_mcp.connection_graph import build_graph
    graph, _edges = build_graph(STATE.parts)
    neighbors_connected = sorted(graph.get(instance_id, set()))

    # Spatial neighbors within `neighbor_studs` in XZ (using AABB)
    nearby: list[str] = []
    if aabb is not None:
        radius = neighbor_studs * 20.0
        (axmin, _, azmin), (axmax, _, azmax) = aabb
        for other_id, other in STATE.parts.items():
            if other_id == instance_id:
                continue
            op = PART_INDEX.get(other.part_id)
            if op is None:
                continue
            o_ab = status["aabbs"].get(other_id)
            if o_ab is None:
                continue
            (oxmin, _, ozmin), (oxmax, _, ozmax) = o_ab
            # Expand our AABB by radius and test overlap in XZ
            if (axmin - radius < oxmax and axmax + radius > oxmin and
                    azmin - radius < ozmax and azmax + radius > ozmin):
                nearby.append(other_id)

    return {
        "instance_id": instance_id,
        "part_id": inst.part_id,
        "part_name": p.name.strip() if p else None,
        "position": [inst.x, inst.y, inst.z],
        "rotation": inst.rotation,
        "subassembly": inst.subassembly,
        "color": inst.color,
        "aabb_world": ({"min": list(aabb[0]), "max": list(aabb[1])}
                        if aabb else None),
        "validation": flags,
        "connected_to": neighbors_connected,
        "collides_with": collides_with,
        "nearby_within_studs": {"radius_studs": neighbor_studs,
                                 "count": len(nearby),
                                 "ids": nearby[:20]},
    }


@mcp.tool()
def collision_detail(part_a: str, part_b: str) -> dict[str, Any]:
    """For two parts known to collide, return overlap region (AABB intersection),
    overlap volume in LDU³, and the smallest translation along each axis that
    would separate them. The LLM picks the cheapest axis."""
    a_inst = STATE.parts.get(part_a)
    b_inst = STATE.parts.get(part_b)
    if a_inst is None or b_inst is None:
        raise ValueError(f"Need two existing instance ids; got {part_a!r}, {part_b!r}")
    a_p = PART_INDEX.get(a_inst.part_id)
    b_p = PART_INDEX.get(b_inst.part_id)
    if a_p is None or b_p is None:
        raise ValueError("Unknown part definition for one of the instances")
    a_aabb = part_aabb_world(a_inst, a_p)
    b_aabb = part_aabb_world(b_inst, b_p)
    inter = _aabb_intersection(a_aabb, b_aabb)
    if inter is None:
        return {"ok": True, "collides": False,
                "message": f"{part_a} and {part_b} do not overlap (AABB-clear)."}
    lo, hi = inter
    volume = (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2])
    sep = _smallest_separation(a_aabb, b_aabb)
    return {
        "ok": True,
        "collides": True,
        "part_a": {"id": part_a, "part_id": a_inst.part_id,
                   "position": [a_inst.x, a_inst.y, a_inst.z]},
        "part_b": {"id": part_b, "part_id": b_inst.part_id,
                   "position": [b_inst.x, b_inst.y, b_inst.z]},
        "overlap_region": {"min": list(lo), "max": list(hi)},
        "overlap_volume_ldu3": round(volume, 2),
        "smallest_separation_ldu": sep,
        "suggestion": _format_separation_suggestion(part_b, sep),
    }


def _format_separation_suggestion(part_id: str, sep: dict[str, float]) -> str:
    if not sep:
        return "Parts do not overlap."
    # Pick the axis with the smallest absolute move
    best_axis, best_move = min(sep.items(), key=lambda kv: abs(kv[1]))
    axis = best_axis.replace("move_", "")
    return f"Move {part_id} by {best_move:+g} LDU on {axis.upper()} to clear (cheapest axis)."


@mcp.tool()
def describe_errors(max_errors: int = 10) -> dict[str, Any]:
    """Walk validate_model()'s errors and return a richer per-error report:
    positions, overlap regions for collisions, suggested fixes. Saves the LLM
    from having to round-trip inspect_part / collision_detail per error."""
    val = validate_model(max_errors=max_errors)
    detailed: list[dict[str, Any]] = []
    for err in val["errors"][:max_errors]:
        t = err["type"]
        if t == "collision":
            try:
                cd = collision_detail(err["instance_a"], err["instance_b"])
                detailed.append({"type": t, **cd})
            except ValueError as e:
                detailed.append({"type": t, "error": str(e), **err})
        elif t in ("floating_part", "unanchored", "invalid_grid_alignment", "unknown_part"):
            iid = err.get("instanceId") or err.get("instance_id")
            try:
                detail = inspect_part(iid, neighbor_studs=2)
                detailed.append({"type": t, "inspect": detail,
                                  "suggestion": err.get("suggestion")})
            except ValueError as e:
                detailed.append({"type": t, "error": str(e), **err})
        else:
            detailed.append(err)
    return {
        "summary": val["summary"],
        "error_count": len(val["errors"]),
        "described": len(detailed),
        "errors": detailed,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _write_startup_placeholder(renders_dir: Path) -> None:
    """Create the renders dir and seed latest.png with a placeholder so the
    user can `open` it before the LLM has rendered anything."""
    renders_dir.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        # Empty file is at least openable as "no preview yet"
        (renders_dir / "latest.png").write_bytes(b"")
        return
    img = Image.new("RGB", (640, 480), (245, 245, 248))
    draw = ImageDraw.Draw(img)
    lines = [
        "LegoMCP",
        f"{len(PART_INDEX)} parts loaded",
        "",
        "Waiting for the first render…",
        "",
        "Ask Claude to build something — every render_model",
        "or render_progress call overwrites this file.",
    ]
    y = 140
    for line in lines:
        # Centered-ish with default font
        draw.text((60, y), line, fill=(60, 60, 70))
        y += 28
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    (renders_dir / "latest.png").write_bytes(buf.getvalue())


def run() -> None:
    """Load the part library (built-in or installed), then serve over stdio."""
    _ensure_library_loaded()
    from lego_mcp.prompts import register_prompts, register_resources
    from lego_mcp.helpers import register_helpers
    register_prompts(mcp)
    register_resources(mcp)
    register_helpers(mcp)
    rd = _renders_dir()
    _write_startup_placeholder(rd)
    log.info("LegoMCP starting. %d parts loaded. Renders -> %s", len(PART_INDEX), rd)
    mcp.run()
