"""High-level building helpers — real LEGO masonry, not stacked boxes.

Goals:
- `build_wall_segment` produces a row-by-row staggered wall (seams shift per
  row); ends are filled with shorter bricks from the palette as needed.
- `build_room` builds a bonded rectangular perimeter by alternating which wall
  direction owns each corner on each row. No standalone corner column.
- All semantic helpers default to strict grid alignment (x, z on the
  half-stud grid, y on plate-aligned positions). Raw `add_part` stays
  permissive.

Conventions:
- Wall thickness = 1 stud (20 LDU). Walls are made of 1×N bricks lying flat,
  long-axis along the wall direction.
- Default brick palette: 1x4 (3010) for body, 1x2 (3004) for ends/fills,
  1x1 (3005) for any single-stud gap.
- "Row" = one brick height (24 LDU = 3 plates). `base_y` is the Y of the
  bottom face of row 0.
"""

from __future__ import annotations

from typing import Any, Iterable

BRICK_H = 24
STUD = 20
PALETTE_DEFAULT_BODY = ("3010", "3004", "3005")   # 1x4, 1x2, 1x1 brick lengths
PALETTE_TWO_STUD_WALL = ("3001", "3002", "3003")  # 2x4, 2x3, 2x2 brick lengths
_BRICK_LENGTH_LDU = {
    # Bricks
    "3010": 80, "3004": 40, "3005": 20, "3622": 60, "3009": 120, "3008": 160,
    # 2-stud-wide bricks usable as wall bricks rotated to be long-along-X
    "3001": 80, "3002": 60, "3003": 40,
    # Plates (same lengths as bricks)
    "3710": 80, "3023": 40, "3024": 20, "3623": 60, "3666": 120, "3460": 160,
    "3020": 80, "3021": 60, "3022": 40,
}


def _server():
    from lego_mcp import server
    return server


# ---------------------------------------------------------------------------
# Brick picker for a single row
# ---------------------------------------------------------------------------

def _pick_brick_run(length_ldu: int, avoid_seam_xs: set[int],
                     palette: Iterable[str] = PALETTE_DEFAULT_BODY,
                     ) -> list[tuple[str, int]] | None:
    """Lay bricks across `length_ldu` so that NO brick boundary lands at any X
    in `avoid_seam_xs` (X positions are relative to the row start, 0 ≤ x ≤ length).

    Returns a list of (part_id, center_offset_from_start) tuples, or None if
    no valid arrangement exists with the given palette.

    Algorithm: greedy + backtrack. Try the largest brick that doesn't land a
    seam on a forbidden X, advance, repeat. Backtrack on dead ends.
    """
    lengths = sorted(((p, _BRICK_LENGTH_LDU[p]) for p in palette),
                      key=lambda x: -x[1])

    def search(cursor: int, plan: list[tuple[str, int]]) -> list[tuple[str, int]] | None:
        if cursor == length_ldu:
            return plan
        if cursor > length_ldu:
            return None
        for pid, blen in lengths:
            seam_x = cursor + blen
            if seam_x > length_ldu:
                continue
            # The seam at cursor+blen must not be in the forbidden set, UNLESS
            # it's the very last seam (cursor + blen == length_ldu — that's the
            # wall end, not a seam between two bricks of this row).
            if seam_x != length_ldu and seam_x in avoid_seam_xs:
                continue
            center = cursor + blen / 2
            result = search(cursor + blen, plan + [(pid, int(center))])
            if result is not None:
                return result
        return None

    return search(0, [])


def _row_seams(plan: list[tuple[str, int]]) -> set[int]:
    """Internal seam X positions of a row plan (positions strictly between
    bricks). Excludes the first/last boundaries which are the row ends."""
    seams: set[int] = set()
    cursor = 0
    for pid, _center in plan:
        cursor += _BRICK_LENGTH_LDU[pid]
        seams.add(cursor)
    seams.discard(0)
    seams.discard(int(plan[-1][1] + _BRICK_LENGTH_LDU[plan[-1][0]] / 2)
                  if False else 0)   # keep cursor end out: plan's final cursor == length
    # Remove the wall-end seam (final cursor): it's the wall boundary, not a stagger target.
    if plan:
        final_end = sum(_BRICK_LENGTH_LDU[pid] for pid, _ in plan)
        seams.discard(final_end)
    return seams


def _plan_seams(plan: list[tuple[str, int]], length: int) -> set[int]:
    """Internal seam positions for a plan of known total length."""
    cursor = 0
    seams: set[int] = set()
    for pid, _ in plan:
        cursor += _BRICK_LENGTH_LDU[pid]
        seams.add(cursor)
    seams.discard(length)
    return seams


def _pick_brick_run_world(length_ldu: int, start_world: float, avoid_world_seams: set[int],
                          palette: Iterable[str]) -> tuple[list[tuple[str, int]], set[int]] | None:
    """Pick a row plan using world-space seam positions for stagger checks."""
    avoid_relative = {
        seam - int(round(start_world))
        for seam in avoid_world_seams
        if 0 < seam - int(round(start_world)) < length_ldu
    }
    plan = _pick_brick_run(length_ldu, avoid_relative, palette=palette)
    if plan is None:
        return None
    relative = _plan_seams(plan, length_ldu)
    return plan, {int(round(start_world)) + seam for seam in relative}


# ---------------------------------------------------------------------------
# Build a single row of bricks
# ---------------------------------------------------------------------------

def _place_row_x(x_start: float, z_center: float, y: float,
                  plan: list[tuple[str, int]], color: str | int) -> list[str]:
    """Place each brick in `plan` along +X starting at x_start, at z=z_center, y=y.
    Bricks are identity-oriented (long axis +X)."""
    s = _server()
    ids = []
    for pid, center_off in plan:
        cx = x_start + center_off
        r = s.add_part(pid, color, cx, y, z_center, rotation="identity")
        ids.append(r["instance_id"])
    return ids


def _place_row_z(x_center: float, z_start: float, y: float,
                  plan: list[tuple[str, int]], color: str | int) -> list[str]:
    """Same as _place_row_x but along +Z. Bricks are rot90y."""
    s = _server()
    ids = []
    for pid, center_off in plan:
        cz = z_start + center_off
        r = s.add_part(pid, color, x_center, y, cz, rotation="rot90y")
        ids.append(r["instance_id"])
    return ids


# ---------------------------------------------------------------------------
# build_wall_segment — straight segment with row-by-row stagger
# ---------------------------------------------------------------------------

def build_wall_segment(start_x: float, start_z: float,
                       end_x: float, end_z: float,
                       height_rows: int = 5,
                       color: str | int = "light_bluish_gray",
                       palette: list[str] | None = None,
                       base_y: float = -4,
                       strict_grid: bool = True,
                       ) -> dict[str, Any]:
    """Lay a straight wall from (start_x, start_z) to (end_x, end_z), staggered.

    Each row chooses a brick arrangement that doesn't share an internal seam
    X with the row below. Short bricks (1x2, 1x1) fill the ends as needed.
    """
    pal = palette or list(PALETTE_DEFAULT_BODY)

    if strict_grid:
        for v, name in ((start_x, "start_x"), (start_z, "start_z"),
                         (end_x, "end_x"), (end_z, "end_z")):
            if abs(v - round(v / 10) * 10) > 0.1:
                raise ValueError(
                    f"{name}={v} not on half-stud grid (must be a multiple of 10 LDU)")
        if abs(base_y - round(base_y / 4) * 4) > 0.1:
            raise ValueError(f"base_y={base_y} not on quarter-plate grid")

    along_x = abs(end_x - start_x) >= abs(end_z - start_z)
    if along_x:
        length = abs(end_x - start_x)
        z_center = start_z
        x0 = min(start_x, end_x)
    else:
        length = abs(end_z - start_z)
        x_center = start_x
        z0 = min(start_z, end_z)
    length = int(round(length))

    placed: list[str] = []
    prev_internal_seams: set[int] = set()
    rows: list[dict[str, Any]] = []

    for row in range(height_rows):
        y = base_y - row * BRICK_H
        plan = _pick_brick_run(length, prev_internal_seams, palette=pal)
        if plan is None:
            return {"ok": False, "reason": f"no valid brick arrangement for row {row}",
                    "length": length, "avoid_seams": sorted(prev_internal_seams),
                    "placed_so_far": len(placed)}
        # Internal seam positions of this row (X relative to x0 or z0).
        cursor = 0
        internal = set()
        for pid, _ in plan:
            cursor += _BRICK_LENGTH_LDU[pid]
            internal.add(cursor)
        internal.discard(length)
        if along_x:
            placed.extend(_place_row_x(x0, z_center, y, plan, color))
        else:
            placed.extend(_place_row_z(x_center, z0, y, plan, color))
        rows.append({"y": y, "bricks": len(plan), "seams": sorted(internal)})
        prev_internal_seams = internal

    return {"ok": True, "bricks_placed": len(placed), "rows": rows,
            "subassembly": _server().STATE.current_subassembly}


# ---------------------------------------------------------------------------
# build_corner — single corner brick per row, alternating rotation
# ---------------------------------------------------------------------------

def build_corner(x: float, z: float, height_rows: int,
                  base_y: float = -4,
                  color: str | int = "light_bluish_gray",
                  brick_part: str = "3004",
                  orientation: str = "alt_x_first",
                  ) -> dict[str, Any]:
    """Place a single brick at (x, z) per row to form an interlocking corner.

    Even rows: brick at identity (long axis +X) — it extends into the X wall.
    Odd rows: brick at rot90y (long axis +Z) — it extends into the Z wall.
    `orientation="alt_z_first"` swaps the parity.

    The wall segments meeting this corner must have their end-insets coordinated
    (see build_room).
    """
    s = _server()
    ids = []
    for row in range(height_rows):
        y = base_y - row * BRICK_H
        # Alternate which axis the corner brick extends along.
        if orientation == "alt_x_first":
            rot = "identity" if row % 2 == 0 else "rot90y"
        else:
            rot = "rot90y" if row % 2 == 0 else "identity"
        r = s.add_part(brick_part, color, x, y, z, rotation=rot)
        ids.append(r["instance_id"])
    return {"ok": True, "corner_bricks": len(ids),
            "subassembly": s.STATE.current_subassembly}


# ---------------------------------------------------------------------------
# build_perimeter — generic rectilinear bonded outline
# ---------------------------------------------------------------------------

def _normalize_points(points: list | tuple) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError("points must be a list of [x, z] pairs")
        out.append((float(p[0]), float(p[1])))
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    if len(out) < 4:
        raise ValueError("perimeter needs at least 4 points")
    return out


def _polygon_area_xz(points: list[tuple[float, float]]) -> float:
    area = 0.0
    for i, (x0, z0) in enumerate(points):
        x1, z1 = points[(i + 1) % len(points)]
        area += x0 * z1 - x1 * z0
    return area / 2


def _validate_perimeter_points(points: list[tuple[float, float]],
                               strict_grid: bool) -> None:
    if abs(_polygon_area_xz(points)) < 0.1:
        raise ValueError("perimeter points must enclose non-zero area")
    for i, (x0, z0) in enumerate(points):
        x1, z1 = points[(i + 1) % len(points)]
        if strict_grid:
            for v, name in ((x0, "x"), (z0, "z")):
                if abs(v - round(v / 10) * 10) > 0.1:
                    raise ValueError(f"point {i} {name}={v} not on half-stud grid")
        if abs(x1 - x0) > 0.1 and abs(z1 - z0) > 0.1:
            raise ValueError(
                f"edge {i} is diagonal; build_perimeter currently needs orthogonal points")


def _corner_kinds(points: list[tuple[float, float]]) -> list[str]:
    """Return 'convex'/'concave' per vertex for an orthogonal polygon."""
    ccw = _polygon_area_xz(points) > 0
    kinds: list[str] = []
    n = len(points)
    for i, (x, z) in enumerate(points):
        px, pz = points[(i - 1) % n]
        nx, nz = points[(i + 1) % n]
        prev_dx, prev_dz = x - px, z - pz
        next_dx, next_dz = nx - x, nz - z
        cross = prev_dx * next_dz - prev_dz * next_dx
        if abs(cross) < 0.1:
            raise ValueError(f"point {i} is collinear; remove redundant perimeter points")
        is_convex = cross > 0 if ccw else cross < 0
        kinds.append("convex" if is_convex else "concave")
    return kinds


def _perimeter_edges(points: list[tuple[float, float]], thickness: float,
                     ) -> list[dict[str, Any]]:
    """Return shifted edge descriptors for a rectilinear outer outline."""
    ccw = _polygon_area_xz(points) > 0
    corner_kinds = _corner_kinds(points)
    edges: list[dict[str, Any]] = []
    for i, (x0, z0) in enumerate(points):
        x1, z1 = points[(i + 1) % len(points)]
        dx, dz = x1 - x0, z1 - z0
        if abs(dx) >= abs(dz):
            sign = 1 if dx > 0 else -1
            # Interior lies left of each directed edge for CCW outlines.
            inward_z = sign if ccw else -sign
            fixed = z0 + inward_z * thickness / 2
            start, end = sorted((x0, x1))
            axis = "x"
            start_vertex = i if x0 <= x1 else (i + 1) % len(points)
            end_vertex = (i + 1) % len(points) if x0 <= x1 else i
        else:
            sign = 1 if dz > 0 else -1
            inward_x = -sign if ccw else sign
            fixed = x0 + inward_x * thickness / 2
            start, end = sorted((z0, z1))
            axis = "z"
            start_vertex = i if z0 <= z1 else (i + 1) % len(points)
            end_vertex = (i + 1) % len(points) if z0 <= z1 else i
        length = end - start
        if length < thickness * 2 - 0.1:
            raise ValueError(
                f"edge {i} is too short ({length:g} LDU) for {thickness:g} LDU thick bonded walls")
        edges.append({
            "name": f"edge_{i}",
            "axis": axis,
            "start": start,
            "end": end,
            "fixed": fixed,
            "length": length,
            "from": [x0, z0],
            "to": [x1, z1],
            "start_corner": corner_kinds[start_vertex],
            "end_corner": corner_kinds[end_vertex],
        })
    return edges


def build_perimeter(points: list,
                    height_rows: int = 5,
                    color: str | int = "light_bluish_gray",
                    base_y: float = -4,
                    thickness_studs: int = 2,
                    palette: list[str] | None = None,
                    strict_grid: bool = True,
                    ) -> dict[str, Any]:
    """Build a bonded rectilinear wall outline from outer-corner points.

    This is the generic primitive behind `build_room`: provide a closed
    orthogonal footprint as `[[x,z], ...]`. Each course alternates axis
    ownership at corners, so row N bridges the corner seams from row N-1.
    """
    if height_rows <= 0:
        raise ValueError("height_rows must be positive")
    if thickness_studs not in (1, 2):
        raise ValueError("thickness_studs currently supports 1 or 2")
    if abs(base_y - round(base_y / 4) * 4) > 0.1:
        raise ValueError(f"base_y={base_y} not on quarter-plate grid")

    pts = _normalize_points(points)
    _validate_perimeter_points(pts, strict_grid)
    thickness = thickness_studs * STUD
    pal = list(palette or (PALETTE_TWO_STUD_WALL if thickness_studs == 2 else PALETTE_DEFAULT_BODY))
    edges = _perimeter_edges(pts, thickness)

    placed_total = 0
    prev_seams: dict[str, set[int]] = {edge["name"]: set() for edge in edges}
    rows: list[dict[str, Any]] = []

    def adjusted_endpoint(value: float, corner_kind: str, owns_corner: bool,
                          is_start: bool) -> float:
        if corner_kind == "convex":
            if owns_corner:
                return value
            return value + thickness if is_start else value - thickness
        # At reentrant corners, the owning course must reach past the nominal
        # vertex so the next course can overlap it. Otherwise L/T-shaped
        # footprints leave visually-near but unbonded corner strips.
        if owns_corner:
            return value - thickness if is_start else value + thickness
        return value

    def place_edge(edge: dict[str, Any], y: float, owns_corner: bool) -> dict[str, Any]:
        nonlocal placed_total
        start = adjusted_endpoint(edge["start"], edge["start_corner"], owns_corner, True)
        end = adjusted_endpoint(edge["end"], edge["end_corner"], owns_corner, False)
        length = int(round(end - start))
        if length <= 0:
            raise ValueError(f"{edge['name']} row at y={y}: no span remains after corner inset")
        picked = _pick_brick_run_world(length, start, prev_seams[edge["name"]], palette=pal)
        if picked is None:
            picked = _pick_brick_run_world(length, start, set(), palette=pal)
        if picked is None:
            raise ValueError(f"{edge['name']} row at y={y}: no brick fit for length={length}")
        plan, world_seams = picked
        if edge["axis"] == "x":
            ids = _place_row_x(start, edge["fixed"], y, plan, color)
        else:
            ids = _place_row_z(edge["fixed"], start, y, plan, color)
        placed_total += len(ids)
        prev_seams[edge["name"]] = world_seams
        return {
            "name": edge["name"],
            "axis": edge["axis"],
            "owns_corner": owns_corner,
            "bricks": len(ids),
            "length": length,
            "start": start,
            "end": end,
            "fixed": edge["fixed"],
            "start_corner": edge["start_corner"],
            "end_corner": edge["end_corner"],
            "seams": sorted(world_seams),
        }

    for row in range(height_rows):
        y = base_y - row * BRICK_H
        owning_axis = "x" if row % 2 == 0 else "z"
        row_segments = [place_edge(edge, y, edge["axis"] == owning_axis)
                        for edge in edges]
        rows.append({"row": row, "y": y, "owning_axis": owning_axis,
                     "segments": row_segments})

    return {
        "ok": True,
        "bricks_placed": placed_total,
        "subassembly": _server().STATE.current_subassembly,
        "rows": rows,
        "points": [[x, z] for x, z in pts],
        "wall_thickness_studs": thickness_studs,
        "palette": pal,
    }


# ---------------------------------------------------------------------------
# build_room — rectangle wrapper around build_perimeter
# ---------------------------------------------------------------------------

def build_room(x_min: float, z_min: float, x_max: float, z_max: float,
                height_rows: int = 5,
                color: str | int = "light_bluish_gray",
                base_y: float = -4,
                strict_grid: bool = True,
                palette: list[str] | None = None,
                ) -> dict[str, Any]:
    """Build a rectangular hollow room with bonded corners.

    Convenience wrapper for `build_perimeter` using rectangular outer points.
    """
    if strict_grid:
        for v, name in ((x_min, "x_min"), (x_max, "x_max"),
                         (z_min, "z_min"), (z_max, "z_max")):
            if abs(v - round(v / 10) * 10) > 0.1:
                raise ValueError(f"{name}={v} not on half-stud grid")
    if x_max <= x_min or z_max <= z_min:
        raise ValueError("room bounds must have positive width and depth")
    return build_perimeter(
        points=[[x_min, z_min], [x_max, z_min], [x_max, z_max], [x_min, z_max]],
        height_rows=height_rows,
        color=color,
        base_y=base_y,
        thickness_studs=2,
        palette=palette,
        strict_grid=strict_grid,
    )


# ---------------------------------------------------------------------------
# Floor + repeat (preserved from prior version)
# ---------------------------------------------------------------------------

def build_floor(x_min: float, z_min: float, x_max: float, z_max: float,
                y: float = -4,
                color: str | int = "light_bluish_gray",
                part_id: str = "3022",
                strict_grid: bool = True,
                ) -> dict[str, Any]:
    """Tile an axis-aligned rectangle with plates."""
    s = _server()
    if strict_grid and any(abs(v - round(v / 10) * 10) > 0.1
                            for v in (x_min, x_max, z_min, z_max)):
        raise ValueError("floor bounds must be on the half-stud grid")
    part = s._require_part(part_id)
    step_x, step_z = part.width, part.depth
    n_x = int((x_max - x_min) // step_x)
    n_z = int((z_max - z_min) // step_z)
    placed = []
    for i in range(n_x):
        for j in range(n_z):
            cx = x_min + step_x / 2 + i * step_x
            cz = z_min + step_z / 2 + j * step_z
            placed.append(s.add_part(part_id, color, cx, y, cz)["instance_id"])
    return {"ok": True, "plates": len(placed), "tiled": [n_x, n_z],
            "subassembly": s.STATE.current_subassembly}


# ---------------------------------------------------------------------------
# Architectural generators: openings + stepped roofs
# ---------------------------------------------------------------------------

def _span_for_opening(opening: dict[str, Any], row: int) -> tuple[float, float] | None:
    bottom = int(opening.get("bottom_row", 0))
    height = int(opening.get("height_rows", opening.get("height", 1)))
    if row < bottom or row >= bottom + height:
        return None

    width = float(opening.get("width", opening.get("width_ldu", STUD * 3)))
    center = float(opening.get("center", opening.get("center_ldu", 0)))
    style = str(opening.get("style", "rect")).lower()
    rel = row - bottom

    if style in ("lancet", "pointed"):
        straight_rows = max(1, int(round(height * 0.58)))
        taper = max(0, rel - straight_rows + 1)
        span_width = width - 2 * STUD * taper
    elif style in ("arch", "round"):
        radius_rows = max(1, min(height // 2, int(round(width / (2 * STUD)))))
        arch_start = height - radius_rows
        taper = max(0, rel - arch_start + 1)
        span_width = width - 2 * STUD * taper
    else:
        span_width = width

    if span_width < STUD * 2 - 0.1:
        return None
    return center - span_width / 2, center + span_width / 2


def _clip_intervals(start: float, end: float,
                    openings: list[tuple[float, float, str | int | None]]
                    ) -> list[tuple[float, float, str | int]]:
    points = {start, end}
    for a, b, _ in openings:
        points.add(max(start, min(end, a)))
        points.add(max(start, min(end, b)))
    cuts = sorted(points)
    intervals: list[tuple[float, float, str | int]] = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < STUD - 0.1:
            continue
        mid = (a + b) / 2
        material: str | int | None = "stone"
        for oa, ob, fill in openings:
            if oa - 0.1 <= mid <= ob + 0.1:
                material = fill
                break
        if material is not None:
            intervals.append((a, b, material))
    return intervals


def build_wall_with_openings(start_x: float, start_z: float,
                             end_x: float, end_z: float,
                             height_rows: int = 8,
                             color: str | int = "light_bluish_gray",
                             openings: list[dict[str, Any]] | None = None,
                             base_y: float = -4,
                             thickness_studs: int = 2,
                             palette: list[str] | None = None,
                             glass_color: str | int = "trans_clear",
                             strict_grid: bool = True,
                             ) -> dict[str, Any]:
    """Build a straight wall whose rows reserve rectangular/arched windows.

    `openings` are measured along the wall's local axis:
    `{"center": 120, "width": 80, "bottom_row": 2, "height_rows": 6,
    "style": "lancet", "fill_color": "trans_clear"}`.

    Set `fill_color` to `None` for a true void. The default transparent fill is
    intentionally structural: glass bricks stack inside the opening so later
    validation still sees supported parts.
    """
    if height_rows <= 0:
        raise ValueError("height_rows must be positive")
    if thickness_studs not in (1, 2):
        raise ValueError("thickness_studs currently supports 1 or 2")
    if strict_grid:
        for v, name in ((start_x, "start_x"), (start_z, "start_z"),
                        (end_x, "end_x"), (end_z, "end_z")):
            if abs(v - round(v / 10) * 10) > 0.1:
                raise ValueError(f"{name}={v} not on half-stud grid")
    if abs(base_y - round(base_y / 4) * 4) > 0.1:
        raise ValueError(f"base_y={base_y} not on quarter-plate grid")

    along_x = abs(end_x - start_x) >= abs(end_z - start_z)
    if along_x:
        axis_start, axis_end = sorted((start_x, end_x))
        fixed = start_z
    else:
        axis_start, axis_end = sorted((start_z, end_z))
        fixed = start_x
    length = int(round(axis_end - axis_start))
    if length <= 0:
        raise ValueError("wall length must be positive")

    pal = list(palette or (PALETTE_TWO_STUD_WALL if thickness_studs == 2 else PALETTE_DEFAULT_BODY))
    opening_specs = openings or []
    prev_seams: set[int] = set()
    placed_total = 0
    rows: list[dict[str, Any]] = []

    for row in range(height_rows):
        y = base_y - row * BRICK_H
        active: list[tuple[float, float, str | int | None]] = []
        for opening in opening_specs:
            span = _span_for_opening(opening, row)
            if span is None:
                continue
            fill = opening.get("fill_color", glass_color)
            a = axis_start + span[0]
            b = axis_start + span[1]
            active.append((max(axis_start, a), min(axis_end, b), fill))

        intervals = _clip_intervals(axis_start, axis_end, active)
        row_segments = []
        row_seams: set[int] = set()
        for a, b, material in intervals:
            span_len = int(round(b - a))
            if span_len <= 0:
                continue
            material_color = color if material == "stone" else material
            picked = _pick_brick_run_world(span_len, a, prev_seams, palette=pal)
            if picked is None:
                picked = _pick_brick_run_world(span_len, a, set(), palette=pal)
            if picked is None:
                raise ValueError(f"row {row}: no brick fit for span length={span_len}")
            plan, seams = picked
            if along_x:
                ids = _place_row_x(a, fixed, y, plan, material_color)
            else:
                ids = _place_row_z(fixed, a, y, plan, material_color)
            placed_total += len(ids)
            row_seams.update(seams)
            row_segments.append({
                "start": a,
                "end": b,
                "material": material,
                "bricks": len(ids),
                "seams": sorted(seams),
            })
        prev_seams = row_seams
        rows.append({"row": row, "y": y, "segments": row_segments})

    return {
        "ok": True,
        "bricks_placed": placed_total,
        "axis": "x" if along_x else "z",
        "rows": rows,
        "subassembly": _server().STATE.current_subassembly,
        "openings": opening_specs,
        "palette": pal,
    }


def _part_footprint(part_id: str, rotation: str) -> tuple[int, int]:
    s = _server()
    part = s._require_part(part_id)
    if rotation.lower() in ("rot90y", "rot270y"):
        return part.depth, part.width
    return part.width, part.depth


def _tile_rect(x_min: float, z_min: float, x_max: float, z_max: float,
               y: float, color: str | int, part_id: str, rotation: str,
               ) -> list[str]:
    s = _server()
    step_x, step_z = _part_footprint(part_id, rotation)
    ids: list[str] = []
    n_x = int((x_max - x_min) // step_x)
    n_z = int((z_max - z_min) // step_z)
    for ix in range(n_x):
        for iz in range(n_z):
            cx = x_min + step_x / 2 + ix * step_x
            cz = z_min + step_z / 2 + iz * step_z
            ids.append(s.add_part(part_id, color, cx, y, cz, rotation=rotation)["instance_id"])
    return ids


def build_stepped_gable_roof(x_min: float, z_min: float, x_max: float, z_max: float,
                             eave_y: float,
                             ridge_axis: str = "z",
                             color: str | int = "dark_bluish_gray",
                             part_id: str = "3001",
                             step_studs: int = 1,
                             max_layers: int | None = None,
                             ) -> dict[str, Any]:
    """Build a stepped pitched roof as two overlapping stepped roof planes.

    `ridge_axis="z"` makes a long nave roof: each higher layer narrows in X.
    `ridge_axis="x"` narrows in Z. This renders clearly in the built-in AABB
    renderer and exports as ordinary LEGO geometry. The default 2x4 brick is
    connector-aware in validation; pass a slope part explicitly when LDraw
    visual slope geometry is more important than current connector diagnostics.
    """
    axis = ridge_axis.lower()
    if axis not in ("x", "z"):
        raise ValueError("ridge_axis must be 'x' or 'z'")
    if step_studs <= 0:
        raise ValueError("step_studs must be positive")
    inset_step = step_studs * STUD
    layer_height = max(1, _server()._require_part(part_id).height)
    rotation = "rot90y" if axis == "z" else "identity"
    foot_x, foot_z = _part_footprint(part_id, rotation)
    band = foot_x if axis == "z" else foot_z
    placed: list[str] = []
    layers = []
    layer = 0
    while True:
        inset = layer * inset_step
        if axis == "z":
            low, high = x_min + inset, x_max - inset
            if high - low < band - 0.1:
                break
            left = (low, z_min, min(low + band, high), z_max)
            right = (max(high - band, low), z_min, high, z_max)
        else:
            low, high = z_min + inset, z_max - inset
            if high - low < band - 0.1:
                break
            left = (x_min, low, x_max, min(low + band, high))
            right = (x_min, max(high - band, low), x_max, high)
        strips = [left]
        if right[0] > left[2] + 0.1 or right[1] > left[3] + 0.1:
            strips.append(right)
        if not strips:
            break
        y = eave_y - layer * layer_height
        layer_ids: list[str] = []
        for sx0, sz0, sx1, sz1 in strips:
            layer_ids.extend(_tile_rect(sx0, sz0, sx1, sz1, y, color, part_id, rotation))
        if not layer_ids:
            break
        placed.extend(layer_ids)
        layers.append({"layer": layer, "y": y, "strips": [list(s) for s in strips],
                       "bounds": [x_min + inset, z_min + inset, x_max - inset, z_max - inset],
                       "parts": len(layer_ids)})
        layer += 1
        if max_layers is not None and layer >= max_layers:
            break
    return {"ok": True, "parts": len(placed), "layers": layers,
            "subassembly": _server().STATE.current_subassembly}


def build_stepped_pyramid_roof(x_min: float, z_min: float, x_max: float, z_max: float,
                               eave_y: float,
                               color: str | int = "dark_bluish_gray",
                               part_id: str = "3003",
                               step_studs: int = 1,
                               max_layers: int | None = None,
                               ) -> dict[str, Any]:
    """Build a stepped four-sided tower roof as overlapping perimeter rings."""
    if step_studs <= 0:
        raise ValueError("step_studs must be positive")
    inset_step = step_studs * STUD
    layer_height = max(1, _server()._require_part(part_id).height)
    foot_x, foot_z = _part_footprint(part_id, "identity")
    band = min(foot_x, foot_z)
    placed: list[str] = []
    layers = []
    layer = 0
    while True:
        inset = layer * inset_step
        low_x, high_x = x_min + inset, x_max - inset
        low_z, high_z = z_min + inset, z_max - inset
        if high_x - low_x < band - 0.1 or high_z - low_z < band - 0.1:
            break
        y = eave_y - layer * layer_height
        strips: list[tuple[float, float, float, float, str]] = [
            (low_x, low_z, min(low_x + band, high_x), high_z, "rot90y"),
        ]
        if high_x - low_x > band + 0.1:
            strips.append((max(high_x - band, low_x), low_z, high_x, high_z, "rot90y"))
        inner_x0 = low_x + band
        inner_x1 = high_x - band
        if inner_x1 - inner_x0 >= STUD - 0.1:
            strips.append((inner_x0, low_z, inner_x1, min(low_z + band, high_z), "identity"))
            if high_z - low_z > band + 0.1:
                strips.append((inner_x0, max(high_z - band, low_z), inner_x1, high_z, "identity"))
        layer_ids: list[str] = []
        for sx0, sz0, sx1, sz1, rotation in strips:
            layer_ids.extend(_tile_rect(sx0, sz0, sx1, sz1, y, color, part_id, rotation))
        if not layer_ids:
            break
        placed.extend(layer_ids)
        layers.append({
            "layer": layer,
            "y": y,
            "bounds": [low_x, low_z, high_x, high_z],
            "strips": [list(s[:4]) for s in strips],
            "parts": len(layer_ids),
        })
        layer += 1
        if max_layers is not None and layer >= max_layers:
            break
    return {"ok": True, "parts": len(placed), "layers": layers,
            "subassembly": _server().STATE.current_subassembly}


def repeat_pattern(part_id: str, count: int,
                    dx: float = 0, dy: float = 0, dz: float = 0,
                    start_x: float = 0, start_y: float = 0, start_z: float = 0,
                    color: str | int = "light_bluish_gray",
                    rotation: str = "identity",
                    ) -> dict[str, Any]:
    s = _server()
    if count <= 0:
        return {"ok": False, "reason": "count must be > 0"}
    ids = []
    for i in range(count):
        r = s.add_part(part_id, color,
                       start_x + i * dx, start_y + i * dy, start_z + i * dz,
                       rotation=rotation)
        ids.append(r["instance_id"])
    return {"ok": True, "placed": len(ids),
            "subassembly": s.STATE.current_subassembly}


# ---------------------------------------------------------------------------
# Placement helpers (LLM-preferred over raw add_part)
# ---------------------------------------------------------------------------

def place_on_top(base_instance_id: str, new_part_id: str,
                  color: str | int = "light_bluish_gray",
                  stud_offset_x: int = 0, stud_offset_z: int = 0,
                  rotation: str = "identity",
                  ) -> dict[str, Any]:
    """Place a new part on top of an existing one.

    `stud_offset_x` and `stud_offset_z` are integer stud offsets relative to
    the base part's center: 0 = directly centered, 1 = shift by 1 stud (20 LDU),
    etc. The new part's Y is computed so it sits exactly on the base's top face.
    """
    s = _server()
    base = s.STATE.parts.get(base_instance_id)
    if base is None:
        raise ValueError(f"No part with instance_id={base_instance_id!r}")
    base_part = s.PART_INDEX.get(base.part_id)
    new_y = base.y - base_part.height          # B's bottom == A's top face
    new_x = base.x + stud_offset_x * STUD
    new_z = base.z + stud_offset_z * STUD
    r = s.add_part(new_part_id, color, new_x, new_y, new_z, rotation=rotation,
                   strict=True)
    return {"ok": True, "instance_id": r["instance_id"], "position": [new_x, new_y, new_z]}


def place_next_to(reference_instance_id: str, new_part_id: str,
                   color: str | int = "light_bluish_gray",
                   side: str = "east",     # north / south / east / west
                   stud_offset: int = 0,
                   rotation: str = "identity",
                   ) -> dict[str, Any]:
    """Place a new part beside an existing one in the same row.

    `side`: north (+Z), south (-Z), east (+X), west (-X). The new part is
    placed flush with the reference part's edge, plus `stud_offset` extra
    studs along the same axis.
    """
    s = _server()
    ref = s.STATE.parts.get(reference_instance_id)
    if ref is None:
        raise ValueError(f"No part with instance_id={reference_instance_id!r}")
    ref_part = s.PART_INDEX.get(ref.part_id)
    new_def_part = s._require_part(new_part_id)
    # Compute the displacement: half of ref's dimension + half of new's dimension.
    if side == "east":
        dx = ref_part.width / 2 + new_def_part.width / 2 + stud_offset * STUD
        new_x, new_y, new_z = ref.x + dx, ref.y, ref.z
    elif side == "west":
        dx = ref_part.width / 2 + new_def_part.width / 2 + stud_offset * STUD
        new_x, new_y, new_z = ref.x - dx, ref.y, ref.z
    elif side == "north":
        dz = ref_part.depth / 2 + new_def_part.depth / 2 + stud_offset * STUD
        new_x, new_y, new_z = ref.x, ref.y, ref.z + dz
    elif side == "south":
        dz = ref_part.depth / 2 + new_def_part.depth / 2 + stud_offset * STUD
        new_x, new_y, new_z = ref.x, ref.y, ref.z - dz
    else:
        raise ValueError(f"side must be north/south/east/west, got {side!r}")
    r = s.add_part(new_part_id, color, new_x, new_y, new_z, rotation=rotation)
    return {"ok": True, "instance_id": r["instance_id"], "position": [new_x, new_y, new_z]}


def find_valid_placements(part_id: str, near_part_id: str) -> dict[str, Any]:
    """List every way `part_id` can connect to the in-model part `near_part_id`."""
    from lego_mcp.connections import find_connections
    s = _server()
    a_inst = s.STATE.parts.get(near_part_id)
    if a_inst is None:
        raise ValueError(f"No part with instance_id={near_part_id!r}")
    a = s.PART_INDEX[a_inst.part_id]
    b = s._require_part(part_id)
    r = find_connections(a, b)
    # Translate from "relative to A at origin" to "absolute world coords".
    abs_placements = []
    for p in r["b_on_a_placements"]:
        abs_placements.append({**p,
                                "world_x": a_inst.x + p["x"],
                                "world_y": a_inst.y + p["y"],
                                "world_z": a_inst.z + p["z"]})
    return {"part_id": part_id, "near_part_id": near_part_id,
            "placements": abs_placements,
            "count": len(abs_placements)}


def suggest_next_brick_for_wall(subassembly: str) -> dict[str, Any]:
    """Heuristic: scan a wall subassembly and propose where the next brick goes
    to extend or close gaps. Phase-1 minimal: returns the top-row brick count
    and the suggested next part_id to use."""
    s = _server()
    parts_in_wall = [p for p in s.STATE.parts.values() if p.subassembly == subassembly]
    if not parts_in_wall:
        return {"ok": False, "reason": f"subassembly {subassembly!r} is empty"}
    top_row = min(p.y for p in parts_in_wall)
    top = [p for p in parts_in_wall if abs(p.y - top_row) < 0.5]
    return {"ok": True, "subassembly": subassembly,
            "top_row_y": top_row,
            "top_row_bricks": len(top),
            "suggested_next": "3010 (1x4 brick) — continue the row, then start a new row above with 1x2 inset to stagger seams"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_helpers(mcp) -> None:
    mcp.tool()(build_wall)
    mcp.tool()(build_wall_segment)
    mcp.tool()(build_corner)
    mcp.tool()(build_perimeter)
    mcp.tool()(build_room)
    mcp.tool()(build_wall_with_openings)
    mcp.tool()(build_stepped_gable_roof)
    mcp.tool()(build_stepped_pyramid_roof)
    mcp.tool()(build_floor)
    mcp.tool()(repeat_pattern)
    mcp.tool()(place_on_top)
    mcp.tool()(place_next_to)
    mcp.tool()(find_valid_placements)
    mcp.tool()(suggest_next_brick_for_wall)


# Back-compat: the old build_wall name still resolves so existing tests +
# example scripts don't break. Delegates to build_wall_segment.
def build_wall(x0: float, z0: float, x1: float, z1: float,
                height_rows: int = 3,
                color: str | int = "light_bluish_gray",
                bond: str = "running",
                brick_part: str = "3001",
                base_y: float = -4,
                inset_ends: float = 0,
                ) -> dict[str, Any]:
    pal = ["3001", "3004"] if brick_part == "3001" else list(PALETTE_DEFAULT_BODY)
    if abs(x1 - x0) >= abs(z1 - z0):
        sx = min(x0, x1) + inset_ends
        ex = max(x0, x1) - inset_ends
        return build_wall_segment(sx, z0, ex, z0, height_rows=height_rows,
                                   color=color, palette=pal, base_y=base_y,
                                   strict_grid=False)
    else:
        sz = min(z0, z1) + inset_ends
        ez = max(z0, z1) - inset_ends
        return build_wall_segment(x0, sz, x0, ez, height_rows=height_rows,
                                   color=color, palette=pal, base_y=base_y,
                                   strict_grid=False)
