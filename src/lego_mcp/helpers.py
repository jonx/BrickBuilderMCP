"""High-level building helpers — encapsulate well-known LEGO techniques.

These exist so the LLM doesn't have to place every brick of a 200-stud wall by
hand. They use the same `add_part`-style mutations under the hood so undo /
checkpoints / validation work normally.

Conventions
-----------
- Walls and floors lay parts in absolute LDU coordinates.
- The helpers' "color" is anything `resolve_color` accepts (name or LDraw ID).
- All new parts inherit the current subassembly tag from STATE.
"""

from __future__ import annotations

from typing import Any


def _server():
    # Lazy import to avoid circular import at module load.
    from lego_mcp import server
    return server


def build_wall(x0: float, z0: float, x1: float, z1: float,
               height_rows: int = 3,
               color: str | int = "light_bluish_gray",
               bond: str = "running",
               brick_part: str = "3001",
               base_y: float = 0,
               ) -> dict[str, Any]:
    """Lay a straight wall from (x0,z0) to (x1,z1).

    Args:
        height_rows: number of brick rows (each row = 24 LDU tall).
        color: wall color.
        bond: "stretcher" (all bricks aligned) or "running" (each row offset
            by half a brick — looks like real masonry, more structurally sound).
        brick_part: defaults to 2x4 brick (3001). Use 3010 (1x4) for thin walls.
        base_y: the y at which the BOTTOM row of bricks sits (so brick 0 has
            top face at base_y - 24 in our convention).

    The wall runs in the dominant axis direction (X or Z) and is 1 brick
    thick perpendicular to that. Returns the count of bricks laid.
    """
    s = _server()
    part = s._require_part(brick_part)
    long_dim = max(part.width, part.depth)
    short_dim = min(part.width, part.depth)
    half = long_dim / 2

    dx, dz = x1 - x0, z1 - z0
    along_x = abs(dx) >= abs(dz)
    length = (dx * dx + dz * dz) ** 0.5
    if length < short_dim:
        return {"ok": False, "reason": "wall too short to fit one brick"}

    # Number of full bricks per row at offset=0
    n_full = int(length // long_dim)
    # For running bond we accept an extra half-brick at one end on alternate rows
    rows: list[int] = []
    laid_ids: list[str] = []

    # Direction along the wall (unit vector). Bricks lie centered on this line.
    # For running bond, alternate rows shift by half a brick along the line.
    BRICK_H = 24

    for row in range(height_rows):
        y = base_y - 4 - row * BRICK_H  # -4 keeps the wall above a baseplate's top
        offset_along = (long_dim / 2) if (bond == "running" and row % 2 == 1) else 0.0
        # Bricks along the wall direction
        n_this_row = n_full - (1 if (offset_along > 0 and (length - offset_along) // long_dim < n_full) else 0)
        if along_x:
            sign = 1 if dx >= 0 else -1
            for i in range(n_this_row):
                cx = x0 + sign * (half + offset_along + i * long_dim)
                # Wall thickness along Z, oriented to span short_dim
                r = s.add_part(brick_part, color, cx, y, z0, rotation="rot90y")
                laid_ids.append(r["instance_id"])
        else:
            sign = 1 if dz >= 0 else -1
            for i in range(n_this_row):
                cz = z0 + sign * (half + offset_along + i * long_dim)
                r = s.add_part(brick_part, color, x0, y, cz)
                laid_ids.append(r["instance_id"])
        rows.append(n_this_row)

    return {"ok": True, "bricks": len(laid_ids), "rows": rows,
            "bond": bond, "subassembly": s.STATE.current_subassembly}


def build_floor(x_min: float, z_min: float, x_max: float, z_max: float,
                y: float = -4,
                color: str | int = "light_bluish_gray",
                part_id: str = "3022",
                ) -> dict[str, Any]:
    """Tile an axis-aligned rectangular area with plates.

    Defaults to 2x2 plates (3022). For larger areas, use 3031 (4x4) or 3036 (6x8).
    Returns the count of plates laid.
    """
    s = _server()
    part = s._require_part(part_id)
    step_x = part.width
    step_z = part.depth
    if step_x <= 0 or step_z <= 0:
        return {"ok": False, "reason": "part has zero dimensions"}

    width = x_max - x_min
    depth = z_max - z_min
    n_x = int(width // step_x)
    n_z = int(depth // step_z)
    laid: list[str] = []
    for i in range(n_x):
        for j in range(n_z):
            cx = x_min + step_x / 2 + i * step_x
            cz = z_min + step_z / 2 + j * step_z
            r = s.add_part(part_id, color, cx, y, cz)
            laid.append(r["instance_id"])
    return {"ok": True, "plates": len(laid), "tiled": [n_x, n_z],
            "subassembly": s.STATE.current_subassembly}


def repeat_pattern(part_id: str, count: int,
                   dx: float = 0, dy: float = 0, dz: float = 0,
                   start_x: float = 0, start_y: float = 0, start_z: float = 0,
                   color: str | int = "light_bluish_gray",
                   rotation: str = "identity",
                   ) -> dict[str, Any]:
    """Place `count` copies of one part along a line.

    Each copy is at (start + i * delta). Useful for crenellations, columns,
    repeated trim, etc.
    """
    s = _server()
    if count <= 0:
        return {"ok": False, "reason": "count must be > 0"}
    ids: list[str] = []
    for i in range(count):
        r = s.add_part(part_id, color,
                       start_x + i * dx, start_y + i * dy, start_z + i * dz,
                       rotation=rotation)
        ids.append(r["instance_id"])
    return {"ok": True, "placed": len(ids),
            "subassembly": s.STATE.current_subassembly}


def register_helpers(mcp) -> None:
    """Attach the helper tools to a FastMCP instance."""
    mcp.tool()(build_wall)
    mcp.tool()(build_floor)
    mcp.tool()(repeat_pattern)
