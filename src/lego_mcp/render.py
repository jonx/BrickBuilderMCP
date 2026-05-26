"""Built-in isometric renderer.

Draws each part's world-space AABB as three shaded parallelograms in classic
"isometric brick" style. Not photoreal — meant for instant visual feedback so
the LLM (or you) can see what's in the model without leaving the chat.

For higher-quality renders, shell out to LDView (planned, not in MVP).
"""

from __future__ import annotations

import io
import math
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from lego_mcp.parts import Part
    from lego_mcp.server import PartInstance

COS30 = math.cos(math.radians(30))
SIN30 = math.sin(math.radians(30))

STUD_RADIUS = 6.0      # LDU (real LEGO stud is 6 LDU radius)
STUD_HEIGHT = 4.0      # LDU
STUD_CIRCLE_SIDES = 12
MAX_STUDS_PER_PART = 256  # skip on baseplate-size parts to keep render fast
FACE_COVER_TOL = 0.5

DEBUG_PALETTE: tuple[tuple[int, int, int], ...] = (
    (226, 74, 51),
    (42, 130, 218),
    (52, 168, 83),
    (245, 178, 47),
    (146, 94, 190),
    (41, 171, 166),
    (238, 112, 162),
    (128, 142, 49),
    (235, 126, 49),
    (77, 109, 186),
    (162, 82, 68),
    (89, 161, 113),
)


def _project(x: float, y: float, z: float) -> tuple[float, float]:
    """Isometric projection. LDraw convention: -Y is up.

    Returns (screen_x, screen_y) where screen_y grows downward (image coords).
    """
    sx = (x - z) * COS30
    sy = (x + z) * SIN30 + y  # +y so that more-negative Y (higher up) -> smaller screen_y
    return sx, sy


def _shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]


def _debug_rgb(inst: "PartInstance", ordinal: int, mode: str,
               model_rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Return the display color for normal/debug render modes."""
    key = mode.strip().lower()
    if inst.part_id in {"3811", "3857"}:
        return model_rgb
    if key in ("model", "actual", "part"):
        return model_rgb
    if key in ("debug", "instance", "piece", "pieces"):
        return DEBUG_PALETTE[ordinal % len(DEBUG_PALETTE)]
    if key in ("row", "course", "courses"):
        row = int(round((-(inst.y + 4)) / 24))
        return DEBUG_PALETTE[row % len(DEBUG_PALETTE)]
    if key in ("rotation", "axis"):
        idx = {
            "identity": 0,
            "rot90y": 1,
            "rot180y": 2,
            "rot270y": 3,
            "rot90x": 4,
            "rot90z": 5,
        }.get(inst.rotation, ordinal)
        return DEBUG_PALETTE[idx % len(DEBUG_PALETTE)]
    raise ValueError("color_mode must be model, instance, row, or rotation")


def _covers_interval(cover_min: float, cover_max: float,
                     target_min: float, target_max: float) -> bool:
    return cover_min <= target_min + FACE_COVER_TOL and cover_max >= target_max - FACE_COVER_TOL


def _face_is_fully_covered(face: str, aabb: tuple, all_aabbs: list[tuple]) -> bool:
    """Cull faces that are exactly hidden by a touching neighboring AABB.

    The renderer only draws the three faces visible from the fixed camera:
    top (-Y), east (+X), and south/front (+Z). If another part shares and
    fully covers one of those planes, drawing that face creates impossible
    internal edges. This is intentionally conservative: partial coverage is
    left to the painter pass so we don't accidentally erase visible surfaces.
    """
    (xmin, ymin, zmin), (xmax, ymax, zmax) = aabb
    for other in all_aabbs:
        if other is aabb:
            continue
        (oxmin, oymin, ozmin), (oxmax, oymax, ozmax) = other
        if face == "top":
            if (abs(oymax - ymin) <= FACE_COVER_TOL
                    and _covers_interval(oxmin, oxmax, xmin, xmax)
                    and _covers_interval(ozmin, ozmax, zmin, zmax)):
                return True
        elif face == "east":
            if (abs(oxmin - xmax) <= FACE_COVER_TOL
                    and _covers_interval(oymin, oymax, ymin, ymax)
                    and _covers_interval(ozmin, ozmax, zmin, zmax)):
                return True
        elif face == "south":
            if (abs(ozmin - zmax) <= FACE_COVER_TOL
                    and _covers_interval(oxmin, oxmax, xmin, xmax)
                    and _covers_interval(oymin, oymax, ymin, ymax)):
                return True
    return False


def _stud_positions_local(part) -> list[tuple[float, float, float]]:
    """Top-stud center positions, in part-local coords.

    Prefers the real positions parsed from the LDraw .dat file. Falls back to
    a grid-based heuristic for built-in-only parts (when the library isn't
    installed). Skips on parts whose name contains 'tile' (smooth top) or that
    would have more than MAX_STUDS_PER_PART studs.
    """
    name_lower = part.name.lower()
    if "tile" in name_lower:
        return []

    # Real stud positions from LDraw geometry (after install-library).
    if part.studs:
        if len(part.studs) > MAX_STUDS_PER_PART:
            return []
        # LDraw convention: bricks extend from y=0 (origin/top) to y=+height (bottom).
        # Our internal convention: origin is at the center of the bottom face,
        # so the top of the brick is at local y = -part.height. The LDraw stud
        # positions have y near 0 (the part's TOP in LDraw frame); we flip the
        # sign and shift so they sit on our top face.
        out = []
        for sx, sy, sz in part.studs:
            # Map LDraw local y (where y=0 is top) to our local y (where -height is top).
            out.append((sx, -part.height + sy, sz))
        return out

    # Heuristic fallback for built-ins. Slopes: only one row of studs.
    nx = max(1, int(round(part.width / 20)))
    nz = max(1, int(round(part.depth / 20)))
    if nx * nz > MAX_STUDS_PER_PART:
        return []
    top_y = -part.height
    if "slope" in name_lower:
        # Approximation: studs on a single back row centered along +Z.
        sz = part.depth / 2 - 10
        return [(-part.width / 2 + 10 + i * 20, top_y, sz) for i in range(nx)]
    out = []
    for i in range(nx):
        for j in range(nz):
            sx = -part.width / 2 + 10 + i * 20
            sz = -part.depth / 2 + 10 + j * 20
            out.append((sx, top_y, sz))
    return out


def _stud_disc_corners(cx: float, cy: float, cz: float) -> list[tuple[float, float, float]]:
    """A flat circle (polygon) at the TOP of a stud cylinder, in world coords."""
    top_y = cy - STUD_HEIGHT  # studs rise toward -Y
    return [
        (cx + STUD_RADIUS * math.cos(2 * math.pi * i / STUD_CIRCLE_SIDES),
         top_y,
         cz + STUD_RADIUS * math.sin(2 * math.pi * i / STUD_CIRCLE_SIDES))
        for i in range(STUD_CIRCLE_SIDES)
    ]


def render_model_png(
    parts: dict[str, "PartInstance"],
    index: dict[str, "Part"],
    width: int = 800,
    height: int = 600,
    background: tuple[int, int, int] = (245, 245, 248),
    margin: int = 40,
    color_mode: str = "model",
    hidden_edges: bool = True,
) -> bytes:
    """Render the model and return PNG bytes."""
    from lego_mcp.parts import color_rgb
    from lego_mcp.server import part_aabb_world

    img = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(img)

    if not parts:
        draw.text((width // 2 - 60, height // 2 - 6), "empty model", fill=(120, 120, 120))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    # Build draw commands for visible face fills plus edge overlays.
    # Painter's algorithm works face-by-face, but a single large face (like a
    # baseplate top) has its centroid far from where small parts sit on top, so
    # naive sorting fails. We subdivide large faces into ~2-stud chunks so the
    # painter sort has finer granularity. Inexpensive for normal models; cathedral
    # scale will want a real z-buffer or BSP. (See NOTES.md.)
    # Camera at (+X, -Y, +Z) -> "closeness" = X - Y + Z. Bigger = closer.
    SUBDIV = 20.0  # LDU; one stud per chunk so studs sort correctly within their own brick.
    Fill = tuple[float, list[tuple[float, float]], tuple[int, int, int]]
    Edge = tuple[float, list[tuple[float, float]], tuple[int, int, int], str]
    faces: list[Fill] = []
    outlines: list[Edge] = []
    hidden_outlines: list[Edge] = []
    all_proj: list[tuple[float, float]] = []

    def _emit_face(corners3d: list[tuple[float, float, float]], fill: tuple[int, int, int]) -> None:
        closeness = sum(cx - cy + cz for (cx, cy, cz) in corners3d) / len(corners3d)
        screen = [_project(*c) for c in corners3d]
        faces.append((closeness, screen, fill))
        all_proj.extend(screen)

    def _split_rect(p0: tuple[float, float, float], du: tuple[float, float, float],
                    dv: tuple[float, float, float], length_u: float, length_v: float,
                    fill: tuple[int, int, int]) -> None:
        nu = max(1, int(math.ceil(length_u / SUBDIV)))
        nv = max(1, int(math.ceil(length_v / SUBDIV)))
        for i in range(nu):
            for j in range(nv):
                u0, u1 = i / nu, (i + 1) / nu
                v0, v1 = j / nv, (j + 1) / nv
                corners = [
                    (p0[0] + du[0]*u0 + dv[0]*v0, p0[1] + du[1]*u0 + dv[1]*v0, p0[2] + du[2]*u0 + dv[2]*v0),
                    (p0[0] + du[0]*u1 + dv[0]*v0, p0[1] + du[1]*u1 + dv[1]*v0, p0[2] + du[2]*u1 + dv[2]*v0),
                    (p0[0] + du[0]*u1 + dv[0]*v1, p0[1] + du[1]*u1 + dv[1]*v1, p0[2] + du[2]*u1 + dv[2]*v1),
                    (p0[0] + du[0]*u0 + dv[0]*v1, p0[1] + du[1]*u0 + dv[1]*v1, p0[2] + du[2]*u0 + dv[2]*v1),
                ]
                _emit_face(corners, fill)

    from lego_mcp.server import matrix_apply, resolve_rotation

    OUTLINE_RGB = (40, 40, 40)
    HIDDEN_RGB = (80, 80, 80)

    def _emit_edge(corners3d: list[tuple[float, float, float]], style: str = "solid") -> None:
        closeness = sum(cx - cy + cz for (cx, cy, cz) in corners3d) / len(corners3d)
        screen = [_project(*c) for c in corners3d]
        target = hidden_outlines if style == "dotted" else outlines
        color = HIDDEN_RGB if style == "dotted" else OUTLINE_RGB
        target.append((closeness, screen, color, style))
        all_proj.extend(screen)

    part_records = []
    for ordinal, inst in enumerate(parts.values()):
        part = index.get(inst.part_id)
        if part is None:
            continue
        aabb = part_aabb_world(inst, part)
        part_records.append((ordinal, inst, part, aabb))
    all_aabbs = [aabb for _ordinal, _inst, _part, aabb in part_records]

    for ordinal, inst, part, aabb in part_records:
        (xmin, ymin, zmin), (xmax, ymax, zmax) = aabb
        rgb = _debug_rgb(inst, ordinal, color_mode, color_rgb(inst.color))
        w, h, d = (xmax - xmin), (ymax - ymin), (zmax - zmin)
        top_covers = _face_is_fully_covered("top", aabb, all_aabbs)
        east_covers = _face_is_fully_covered("east", aabb, all_aabbs)
        south_covers = _face_is_fully_covered("south", aabb, all_aabbs)

        top = [(xmin, ymin, zmin), (xmax, ymin, zmin),
               (xmax, ymin, zmax), (xmin, ymin, zmax)]
        east = [(xmax, ymin, zmin), (xmax, ymin, zmax),
                (xmax, ymax, zmax), (xmax, ymax, zmin)]
        south = [(xmin, ymin, zmax), (xmax, ymin, zmax),
                 (xmax, ymax, zmax), (xmin, ymax, zmax)]

        if top_covers:
            if hidden_edges:
                _emit_edge(top, "dotted")
        else:
            _split_rect(p0=(xmin, ymin, zmin), du=(w, 0, 0), dv=(0, 0, d),
                        length_u=w, length_v=d, fill=_shade(rgb, 1.10))   # top
            _emit_edge(top)

        if east_covers:
            if hidden_edges:
                _emit_edge(east, "dotted")
        else:
            _split_rect(p0=(xmax, ymin, zmin), du=(0, h, 0), dv=(0, 0, d),
                        length_u=h, length_v=d, fill=_shade(rgb, 0.85))   # east/right
            _emit_edge(east)

        if south_covers:
            if hidden_edges:
                _emit_edge(south, "dotted")
        else:
            _split_rect(p0=(xmin, ymin, zmax), du=(w, 0, 0), dv=(0, h, 0),
                        length_u=w, length_v=h, fill=_shade(rgb, 0.70))   # south/front
            _emit_edge(south)

        # Studs on top, if applicable. Each stud is a small disc at the top of
        # a 4-LDU-tall cylinder. We project the disc as a polygon and feed it
        # to the painter sort so stacked bricks correctly cover studs below.
        rot = resolve_rotation(inst.rotation)
        # Studs slightly brighter than the top face so they read as raised.
        stud_fill = _shade(rgb, 1.25)
        if not top_covers:
            for sx_local, sy_local, sz_local in _stud_positions_local(part):
                wx, wy, wz = matrix_apply(rot, (sx_local, sy_local, sz_local))
                world_center = (wx + inst.x, wy + inst.y, wz + inst.z)
                disc = _stud_disc_corners(*world_center)
                _emit_face(disc, stud_fill)

    if not all_proj:
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    # Camera-fit transform.
    pxs = [p[0] for p in all_proj]
    pys = [p[1] for p in all_proj]
    src_w = max(1.0, max(pxs) - min(pxs))
    src_h = max(1.0, max(pys) - min(pys))
    scale = min((width - 2 * margin) / src_w, (height - 2 * margin) / src_h)
    off_x = margin - min(pxs) * scale + (width - 2 * margin - src_w * scale) / 2
    off_y = margin - min(pys) * scale + (height - 2 * margin - src_h * scale) / 2

    def _screen_poly(screen: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(sx * scale + off_x, sy * scale + off_y) for sx, sy in screen]

    def _draw_dotted_line(p0: tuple[float, float], p1: tuple[float, float],
                          color: tuple[int, int, int], width_px: int = 1) -> None:
        x0, y0 = p0
        x1, y1 = p1
        dx, dy = x1 - x0, y1 - y0
        dist = math.hypot(dx, dy)
        if dist <= 0:
            return
        dash, gap = 4.0, 4.0
        cursor = 0.0
        while cursor < dist:
            end = min(cursor + dash, dist)
            a = cursor / dist
            b = end / dist
            draw.line(
                [(x0 + dx * a, y0 + dy * a), (x0 + dx * b, y0 + dy * b)],
                fill=color,
                width=width_px,
            )
            cursor += dash + gap

    def _draw_dotted_poly(poly: list[tuple[float, float]],
                          color: tuple[int, int, int]) -> None:
        for i, p0 in enumerate(poly):
            _draw_dotted_line(p0, poly[(i + 1) % len(poly)], color)

    # Merge sub-fills + outlines by depth and draw farthest-first.
    # Sub-fills are tagged with fill_only=True (no outline stroke); outlines
    # are stroke-only (no fill).
    merged: list[tuple[float, list[tuple[float, float]], tuple[int, int, int], str]] = []
    for closeness, screen, fill in faces:
        merged.append((closeness, screen, fill, "fill"))
    for closeness, screen, outline_rgb, style in outlines:
        merged.append((closeness, screen, outline_rgb, style))
    merged.sort(key=lambda f: f[0])
    for _, screen, color, style in merged:
        poly = _screen_poly(screen)
        if style == "fill":
            draw.polygon(poly, fill=color, outline=color)
        else:
            draw.line(poly + [poly[0]], fill=color, width=1)

    # Hidden/internal contact planes are intentionally drawn last as dotted
    # guide lines. They are not visible surfaces; drawing them last makes that
    # structural information available without pretending it is an outside edge.
    for _closeness, screen, color, _style in hidden_outlines:
        _draw_dotted_poly(_screen_poly(screen), color)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
