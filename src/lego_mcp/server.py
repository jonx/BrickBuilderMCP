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
class ModelState:
    name: str = "untitled"
    parts: dict[str, PartInstance] = field(default_factory=dict)
    _next_id: int = 1
    # Undo/redo: stacks of (description, parts-dict-snapshot).
    # Snapshot-based (not inverse-op) for simplicity. The cost is one deepcopy per
    # mutation; for >10k-part models this becomes the bottleneck and we'll switch
    # to inverse ops (see NOTES.md).
    _undo: list[tuple[str, dict[str, "PartInstance"]]] = field(default_factory=list)
    _redo: list[tuple[str, dict[str, "PartInstance"]]] = field(default_factory=list)
    _checkpoints: dict[str, "ModelState"] = field(default_factory=dict)

    def new_id(self) -> str:
        i = str(self._next_id)
        self._next_id += 1
        return i


STATE = ModelState()
PART_INDEX: dict[str, Part] = dict(BUILTIN_PARTS)
UNDO_LIMIT = 200


def _snapshot_for_undo(description: str) -> None:
    """Record the current parts dict on the undo stack, before a mutation."""
    STATE._undo.append((description, deepcopy(STATE.parts)))
    STATE._redo.clear()
    if len(STATE._undo) > UNDO_LIMIT:
        STATE._undo = STATE._undo[-UNDO_LIMIT:]


def _require_part(part_id: str) -> Part:
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


@mcp.tool()
def create_model(name: str = "untitled") -> dict[str, Any]:
    """Start a fresh, empty model. Clears parts, undo, redo, and checkpoints."""
    global STATE
    STATE = ModelState(name=name)
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
    _snapshot_for_undo(f"add {part.part_id}")
    inst_id = STATE.new_id()
    inst = PartInstance(instance_id=inst_id, part_id=part.part_id, color=cid,
                        x=float(x), y=float(y), z=float(z), rotation=rotation.lower())
    STATE.parts[inst_id] = inst
    return {"ok": True, "instance_id": inst_id, "part": _inst_dict(inst)}


@mcp.tool()
def remove_part(instance_id: str) -> dict[str, Any]:
    """Remove a part by its instance ID. Undoable."""
    if instance_id not in STATE.parts:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    _snapshot_for_undo(f"remove {instance_id}")
    STATE.parts.pop(instance_id)
    return {"ok": True, "removed": instance_id}


@mcp.tool()
def move_part(instance_id: str, x: float, y: float, z: float) -> dict[str, Any]:
    """Reposition a part to a new (x,y,z) in LDU. Undoable."""
    inst = STATE.parts.get(instance_id)
    if inst is None:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    _snapshot_for_undo(f"move {instance_id}")
    inst.x, inst.y, inst.z = float(x), float(y), float(z)
    return {"ok": True, "part": _inst_dict(inst)}


@mcp.tool()
def rotate_part(instance_id: str, rotation: str) -> dict[str, Any]:
    """Change a part's rotation. Undoable."""
    inst = STATE.parts.get(instance_id)
    if inst is None:
        raise ValueError(f"No part with instance_id={instance_id!r}")
    resolve_rotation(rotation)
    _snapshot_for_undo(f"rotate {instance_id}")
    inst.rotation = rotation.lower()
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
def validate_model() -> dict[str, Any]:
    """Check for unknown parts and AABB collisions. Returns a structured report."""
    errors: list[dict[str, Any]] = []
    aabbs: dict[str, tuple] = {}
    for inst in STATE.parts.values():
        part = PART_INDEX.get(inst.part_id)
        if part is None:
            errors.append({"type": "unknown_part", "part_id": inst.part_id,
                           "instance_id": inst.instance_id})
            continue
        aabbs[inst.instance_id] = part_aabb_world(inst, part)
    ids = list(aabbs.keys())
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if aabbs_overlap(aabbs[a], aabbs[b]):
                errors.append({"type": "collision", "instance_a": a, "instance_b": b,
                               "note": "AABB-based; tiles on studs may falsely overlap."})
    return {
        "valid": not errors,
        "errors": errors,
        "summary": {
            "parts": len(STATE.parts),
            "collisions": sum(1 for e in errors if e["type"] == "collision"),
            "unknown_parts": sum(1 for e in errors if e["type"] == "unknown_part"),
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
    global STATE
    text = Path(path).expanduser().read_text()
    instances = parse_ldr_text(text)
    STATE = ModelState(name=Path(path).stem)
    for inst in instances:
        inst.instance_id = STATE.new_id()
        STATE.parts[inst.instance_id] = inst
    return {"ok": True, "loaded": len(instances), "model": STATE.name}


@mcp.tool()
def undo() -> dict[str, Any]:
    """Undo the last mutation."""
    if not STATE._undo:
        return {"ok": False, "reason": "nothing to undo"}
    desc, prev_parts = STATE._undo.pop()
    STATE._redo.append((desc, deepcopy(STATE.parts)))
    STATE.parts = prev_parts
    return {"ok": True, "undone": desc, "parts": len(STATE.parts)}


@mcp.tool()
def redo() -> dict[str, Any]:
    """Redo the last undone mutation."""
    if not STATE._redo:
        return {"ok": False, "reason": "nothing to redo"}
    desc, next_parts = STATE._redo.pop()
    STATE._undo.append((desc, deepcopy(STATE.parts)))
    STATE.parts = next_parts
    return {"ok": True, "redone": desc, "parts": len(STATE.parts)}


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
    global STATE
    snap = STATE._checkpoints.get(name)
    if not snap:
        raise ValueError(f"No checkpoint {name!r}. Saved: {sorted(STATE._checkpoints)}")
    saved_checkpoints = STATE._checkpoints
    STATE = deepcopy(snap)
    STATE._checkpoints = saved_checkpoints
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
    global PART_INDEX
    PART_INDEX = load_library_index()
    log.info("LegoMCP starting. %d parts loaded.", len(PART_INDEX))
    mcp.run()
