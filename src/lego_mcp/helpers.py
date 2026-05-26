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
               inset_ends: float = 0,
               ) -> dict[str, Any]:
    """Lay a straight wall from (x0,z0) to (x1,z1).

    Args:
        height_rows: number of brick rows (each row = 24 LDU tall).
        color: wall color.
        bond: "stretcher" (all bricks aligned) or "running" (each row offset
            by half a brick — looks like real masonry, more structurally sound).
        brick_part: defaults to 2x4 brick (3001). Use 3010 (1x4) for thin walls.
        base_y: Y of the top of the supporting surface (where row 0 sits).
            For a brick standing on the ground plane, use 0 (default). For a
            brick sitting on a 1-LDU-thick baseplate at y=0, use -1.
        inset_ends: shorten the wall at each end by this many LDU so that two
            perpendicular walls meeting at a corner don't overlap. For 4 walls
            around a square room with 2x4 bricks (40 LDU thick), set
            inset_ends=20 (half the wall thickness) on the wall pairs that
            meet at corners.

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
    length = (dx * dx + dz * dz) ** 0.5 - 2 * inset_ends
    if length < short_dim:
        return {"ok": False, "reason": "wall too short to fit one brick after inset"}

    n_full = int(length // long_dim)
    rows: list[int] = []
    laid_ids: list[str] = []
    BRICK_H = 24

    # For a 2x4 brick at identity, long axis is +X. Use identity for X-running
    # walls; rotate to rot90y so the long axis aligns with Z for Z-running walls.
    if along_x:
        sign = 1 if dx >= 0 else -1
        start_x = x0 + sign * inset_ends
        rotation = "identity"
    else:
        sign = 1 if dz >= 0 else -1
        start_z = z0 + sign * inset_ends
        rotation = "rot90y"

    for row in range(height_rows):
        y = base_y - row * BRICK_H
        offset_along = half if (bond == "running" and row % 2 == 1) else 0.0
        n_this_row = n_full
        # Running-bond offset rows fit one fewer full brick if it'd overshoot.
        if offset_along > 0 and (n_full * long_dim + offset_along) > length:
            n_this_row = max(0, n_full - 1)
        if along_x:
            for i in range(n_this_row):
                cx = start_x + sign * (half + offset_along + i * long_dim)
                r = s.add_part(brick_part, color, cx, y, z0, rotation=rotation)
                laid_ids.append(r["instance_id"])
        else:
            for i in range(n_this_row):
                cz = start_z + sign * (half + offset_along + i * long_dim)
                r = s.add_part(brick_part, color, x0, y, cz, rotation=rotation)
                laid_ids.append(r["instance_id"])
        rows.append(n_this_row)

    return {"ok": True, "bricks": len(laid_ids), "rows": rows,
            "bond": bond, "subassembly": s.STATE.current_subassembly}


def build_room(x_min: float, z_min: float, x_max: float, z_max: float,
               height_rows: int = 5,
               color: str | int = "light_bluish_gray",
               bond: str = "running",
               base_y: float = -4,
               brick_part: str = "3001",
               ) -> dict[str, Any]:
    """Build a rectangular hollow room: 4 walls + 2x2 corner stacks.

    The 4 walls are inset 20 LDU at each end; 2x2 corner blocks (3003) fill
    each corner so the AABB perimeter is closed and stays inside the
    (x_min, x_max) x (z_min, z_max) boundary.

    LIMITATION — this is NOT real LEGO masonry corner bonding. The corner
    blocks form vertical columns that bond up/down to themselves but only
    SIT NEXT TO the perpendicular walls, they don't interlock with them.
    Real bonded corners need bricks that span the corner per row, alternating
    direction (even rows wrap one way, odd rows the other). Generic helper
    can't safely do this without protruding past the boundary, so we ship
    the simple version. For a structurally-bonded corner the LLM/user should
    place corner pieces manually with `add_part` per row.

    Defaults assume the room sits on a baseplate at y=0 (top y=-1).
    """
    s = _server()
    wall_thickness = 40
    inset = wall_thickness / 2  # 20 LDU = half the wall thickness

    wall_results = [
        build_wall(x_min, z_min, x_max, z_min, height_rows, color, bond,
                   brick_part, base_y, inset_ends=inset),
        build_wall(x_min, z_max, x_max, z_max, height_rows, color, bond,
                   brick_part, base_y, inset_ends=inset),
        build_wall(x_min, z_min, x_min, z_max, height_rows, color, bond,
                   brick_part, base_y, inset_ends=inset),
        build_wall(x_max, z_min, x_max, z_max, height_rows, color, bond,
                   brick_part, base_y, inset_ends=inset),
    ]
    wall_bricks = sum(w.get("bricks", 0) for w in wall_results)

    BRICK_H = 24
    corners = [(x_min, z_min), (x_max, z_min), (x_min, z_max), (x_max, z_max)]
    corner_count = 0
    for (cx, cz) in corners:
        for row in range(height_rows):
            y = base_y - row * BRICK_H
            s.add_part("3003", color, cx, y, cz)  # 2x2 corner brick
            corner_count += 1

    return {"ok": True, "wall_bricks": wall_bricks, "corner_bricks": corner_count,
            "subassembly": s.STATE.current_subassembly,
            "warning": "Corner columns don't bond into walls — see helper docstring."}


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
    mcp.tool()(build_room)
    mcp.tool()(build_floor)
    mcp.tool()(repeat_pattern)
