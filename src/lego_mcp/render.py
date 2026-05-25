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


def _project(x: float, y: float, z: float) -> tuple[float, float]:
    """Isometric projection. LDraw convention: -Y is up.

    Returns (screen_x, screen_y) where screen_y grows downward (image coords).
    """
    sx = (x - z) * COS30
    sy = (x + z) * SIN30 + y  # +y so that more-negative Y (higher up) -> smaller screen_y
    return sx, sy


def _shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)  # type: ignore[return-value]


def render_model_png(
    parts: dict[str, "PartInstance"],
    index: dict[str, "Part"],
    width: int = 800,
    height: int = 600,
    background: tuple[int, int, int] = (245, 245, 248),
    margin: int = 40,
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

    # Build a list of (depth, screen_corners, fill_rgb, outline) per visible face.
    # Painter's algorithm works face-by-face, but a single large face (like a
    # baseplate top) has its centroid far from where small parts sit on top, so
    # naive sorting fails. We subdivide large faces into ~2-stud chunks so the
    # painter sort has finer granularity. Inexpensive for normal models; cathedral
    # scale will want a real z-buffer or BSP. (See NOTES.md.)
    # Camera at (+X, -Y, +Z) -> "closeness" = X - Y + Z. Bigger = closer.
    SUBDIV = 40.0  # LDU; matches a 2x2 brick footprint.
    Face = tuple[float, list[tuple[float, float]], tuple[int, int, int], tuple[int, int, int] | None]
    faces: list[Face] = []
    all_proj: list[tuple[float, float]] = []

    def _emit_face(corners3d: list[tuple[float, float, float]], fill: tuple[int, int, int]) -> None:
        closeness = sum(cx - cy + cz for (cx, cy, cz) in corners3d) / len(corners3d)
        screen = [_project(*c) for c in corners3d]
        faces.append((closeness, screen, fill, None))
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

    for inst in parts.values():
        part = index.get(inst.part_id)
        if part is None:
            continue
        (xmin, ymin, zmin), (xmax, ymax, zmax) = part_aabb_world(inst, part)
        rgb = color_rgb(inst.color)
        w, h, d = (xmax - xmin), (ymax - ymin), (zmax - zmin)

        # Subdivide each visible face into ~2-stud chunks so painter's depth
        # sorting resolves stacking correctly.
        # Top face (y=ymin): vary in X and Z.
        _split_rect(p0=(xmin, ymin, zmin), du=(w, 0, 0), dv=(0, 0, d),
                    length_u=w, length_v=d, fill=_shade(rgb, 1.10))
        # Right face (x=xmax): vary in Y and Z.
        _split_rect(p0=(xmax, ymin, zmin), du=(0, h, 0), dv=(0, 0, d),
                    length_u=h, length_v=d, fill=_shade(rgb, 0.85))
        # Front face (z=zmax): vary in X and Y.
        _split_rect(p0=(xmin, ymin, zmax), du=(w, 0, 0), dv=(0, h, 0),
                    length_u=w, length_v=h, fill=_shade(rgb, 0.70))

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

    def _to_screen(x: float, y: float, z: float) -> tuple[float, float]:
        sx, sy = _project(x, y, z)
        return sx * scale + off_x, sy * scale + off_y

    # Pass 1: draw all sub-faces farthest-first (no outlines — they'd litter the image).
    faces.sort(key=lambda f: f[0])
    for _, screen, fill, _outline in faces:
        poly = [(sx * scale + off_x, sy * scale + off_y) for sx, sy in screen]
        draw.polygon(poly, fill=fill)

    # Pass 2: outline each part's silhouette on top, so edges read clearly.
    # We outline the 9 visible edges of each part's AABB. Outlines drawn on top
    # will sometimes appear over occluding parts — visually fine for LEGO style.
    edge_color = (40, 40, 40)
    for inst in parts.values():
        part = index.get(inst.part_id)
        if part is None:
            continue
        (xmin, ymin, zmin), (xmax, ymax, zmax) = part_aabb_world(inst, part)
        edges = [
            # Top face perimeter
            ((xmin, ymin, zmin), (xmax, ymin, zmin)),
            ((xmax, ymin, zmin), (xmax, ymin, zmax)),
            ((xmax, ymin, zmax), (xmin, ymin, zmax)),
            ((xmin, ymin, zmax), (xmin, ymin, zmin)),
            # Verticals on the visible side
            ((xmax, ymin, zmin), (xmax, ymax, zmin)),
            ((xmax, ymin, zmax), (xmax, ymax, zmax)),
            ((xmin, ymin, zmax), (xmin, ymax, zmax)),
            # Bottom-front and bottom-right
            ((xmax, ymax, zmin), (xmax, ymax, zmax)),
            ((xmin, ymax, zmax), (xmax, ymax, zmax)),
        ]
        for p0, p1 in edges:
            draw.line([_to_screen(*p0), _to_screen(*p1)], fill=edge_color, width=1)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
