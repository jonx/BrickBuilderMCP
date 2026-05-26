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
    p = PART_INDEX.get(part_id) or PART_INDEX.get(part_id.lower())
    if p is None:
        # First miss: load the full library and retry, in case the user installed it.
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


def emit_ldr(state: ModelState) -> str:
    """Emit a single-file .ldr body (no FILE markers)."""
    out = [f"0 {state.name}", "0 Generated by LegoMCP", "0 Name: " + state.name + ".ldr", ""]
    for inst in state.parts.values():
        part = PART_INDEX.get(inst.part_id) or BUILTIN_PARTS.get(inst.part_id)
        # Even if part is unknown, we still emit (LDraw viewer will report it).
        m = resolve_rotation(inst.rotation)
        line = (f"1 {inst.color} {inst.x:g} {inst.y:g} {inst.z:g} "
                + " ".join(f"{v:g}" for v in m)
                + f" {inst.part_id}.dat")
        out.append(line)
    return "\n".join(out) + "\n"


def emit_mpd(state: ModelState) -> str:
    """Wrap the model in an MPD 0 FILE block. Phase 1 emits a single block."""
    main = f"main.ldr"
    body = emit_ldr(state)
    return f"0 FILE {main}\n{body}0 NOFILE\n"


def parse_ldr_text(text: str) -> list[PartInstance]:
    """Parse type-1 lines from .ldr or .mpd text. Multi-block MPDs: all type-1 lines flattened.

    Returns PartInstance objects with fresh instance IDs (caller assigns).
    """
    out: list[PartInstance] = []
    counter = itertools.count(1)
    for raw in text.splitlines():
        m = _TYPE1_RE.match(raw)
        if not m:
            continue
        color = int(m.group(1))
        x, y, z = float(m.group(2)), float(m.group(3)), float(m.group(4))
        mat = tuple(float(m.group(i)) for i in range(5, 14))
        part_path = m.group(14).strip()
        part_id = Path(part_path).stem
        rot_name = _matrix_to_rotation_name(mat)  # type: ignore[arg-type]
        out.append(PartInstance(
            instance_id=str(next(counter)),
            part_id=part_id,
            color=color,
            x=x, y=y, z=z,
            rotation=rot_name,
        ))
    return out


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
) -> dict[str, Any]:
    """Place a brick. Returns the new instance_id.

    Args:
        part_id: LDraw part number (e.g. "3001" for a 2x4 brick).
        color: Color name (e.g. "red") or LDraw color ID (e.g. 4).
        x, y, z: Position in LDU. Origin = center of part's bottom face. **-Y is up.**
        rotation: One of identity, rot90y, rot180y, rot270y, rot90x, rot90z.
    """
    part = _require_part(part_id)
    cid = resolve_color(color)
    resolve_rotation(rotation)  # validate name
    inst_id = STATE.new_id()
    inst = PartInstance(instance_id=inst_id, part_id=part.part_id, color=cid,
                        x=float(x), y=float(y), z=float(z), rotation=rotation.lower())
    STATE.parts[inst_id] = inst
    _record(Op("add", inst_id, {"inst": deepcopy(inst)}))
    return {"ok": True, "instance_id": inst_id, "part": _inst_dict(inst)}


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
def list_parts(limit: int = 200) -> dict[str, Any]:
    """List parts currently in the model (up to `limit`)."""
    items = list(STATE.parts.values())[:limit]
    return {
        "model": STATE.name,
        "total": len(STATE.parts),
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


@mcp.tool()
def validate_model(max_errors: int = 200) -> dict[str, Any]:
    """Check for unknown parts and AABB collisions. Returns a structured report.

    Uses spatial grid bucketing so collision detection scales to thousands of
    parts (we only test pairs that share at least one ~2x4-brick cell).
    Caps the returned error list at `max_errors`; counts still cover everything.
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

    return {
        "valid": collision_count == 0 and unknown_count == 0,
        "errors": errors,
        "summary": {
            "parts": len(STATE.parts),
            "collisions": collision_count,
            "unknown_parts": unknown_count,
            "errors_truncated": collision_count + unknown_count > len(errors),
        },
    }


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
    log.info("LegoMCP starting. %d parts loaded.", len(PART_INDEX))
    mcp.run()
