"""Enumerate all the ways two LEGO parts can connect.

Connection model
----------------
LDraw .dat files give us each part's TOP STUDS (where its studs poke up) plus
the internal CLUTCH TUBES (stud4a primitives — the vertical tubes inside a
brick). But for connection purposes, what matters is the part's BOTTOM-FACE
RECEPTORS: the XZ positions where a stud from below can be inserted.

For a standard system brick or plate, the bottom receptors lie on the same
20-LDU grid as the studs — a 2x4 brick has receptor positions at the 8
spots corresponding to a 2x4 stud pattern, regardless of how the internal
tubes are arranged. We derive receptors from the part's footprint:

    nx = round(width / 20),  nz = round(depth / 20)
    receptors = (i*20 - width/2 + 10, 0, j*20 - depth/2 + 10)
                for i in 0..nx-1, j in 0..nz-1

For TILES (no top studs but still has a bottom face that mates with studs
from below), the receptor grid is still derived from the footprint.

For BASEPLATES (no anti-studs / receptors on bottom — they sit on the table),
we skip generating receptors.

This phase covers stud-mating only. Pins / hinges / clips need !LDCAD CONN
parsing later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lego_mcp.parts import Part

TOL = 0.5  # LDU. Position-equality tolerance.


def receptor_positions(part: Part) -> list[tuple[float, float, float]]:
    """The grid of stud-receptors on this part's BOTTOM face, in local coords.

    Empty for baseplates (smooth bottom — they don't sit on anything).
    """
    name_lower = part.name.lower()
    if "baseplate" in name_lower:
        return []
    nx = max(1, int(round(part.width / 20)))
    nz = max(1, int(round(part.depth / 20)))
    receptors = []
    for i in range(nx):
        for j in range(nz):
            rx = i * 20 - part.width / 2 + 10
            rz = j * 20 - part.depth / 2 + 10
            receptors.append((rx, 0.0, rz))
    return receptors


@dataclass(frozen=True)
class Placement:
    """B placed at (x, y, z) with `rotation` on top of A at origin.

    `studs_matched` is the number of anti-studs of B that land on a stud of A.
    A "full" placement has studs_matched == len(B.antistuds) — every anti-stud
    is filled, i.e. the part is fully connected.
    """
    x: float
    y: float
    z: float
    rotation: str
    studs_matched: int
    direction: str  # "B-on-A" or "A-on-B"


_Y_ROTATIONS: list[tuple[str, tuple[float, float, float, float, float, float, float, float, float]]] = [
    ("identity", (1, 0, 0, 0, 1, 0, 0, 0, 1)),
    ("rot90y",   (0, 0, 1, 0, 1, 0, -1, 0, 0)),
    ("rot180y",  (-1, 0, 0, 0, 1, 0, 0, 0, -1)),
    ("rot270y",  (0, 0, -1, 0, 1, 0, 1, 0, 0)),
]


def _apply(mat: tuple[float, ...], p: tuple[float, float, float]) -> tuple[float, float, float]:
    return (mat[0] * p[0] + mat[1] * p[1] + mat[2] * p[2],
            mat[3] * p[0] + mat[4] * p[1] + mat[5] * p[2],
            mat[6] * p[0] + mat[7] * p[1] + mat[8] * p[2])


def _quantize(p: tuple[float, float, float]) -> tuple[int, int, int]:
    """Quantize to integer LDU for stud-position set membership."""
    return (int(round(p[0])), int(round(p[1])), int(round(p[2])))


def find_placements_b_on_a(a: Part, b: Part, *,
                            min_studs_matched: int = 1) -> list[Placement]:
    """Enumerate every placement of B sitting on top of A with at least
    `min_studs_matched` receptor-to-stud matches.

    Set min_studs_matched=1 (default) to include partial-overlap placements
    that LEGO running bond relies on. Set =len(B.receptors) for FULL nesting
    only (B's whole footprint inside A's stud area).

    Algorithm:
      For each Y-rotation r of B:
        Each (a_stud_xz, b_receptor_xz) pair gives a candidate translation T
        with T.y = -A.height (so B's bottom face sits on A's top face).
        Count how many of B's rotated receptors at +T land on A studs;
        keep T if count >= min_studs_matched.
    """
    a_studs = list(a.studs)
    if not a_studs:
        return []
    receptors_local = receptor_positions(b)
    if not receptors_local:
        return []
    a_stud_xz = {(int(round(s[0])), int(round(s[2]))) for s in a_studs}
    ty = -float(a.height)

    placements: list[Placement] = []
    seen: set[tuple[int, int, int, str]] = set()

    for rot_name, rmat in _Y_ROTATIONS:
        rotated = [_apply(rmat, p) for p in receptors_local]
        for a_stud in a_studs:
            asx, asz = a_stud[0], a_stud[2]
            for r in rotated:
                tx = asx - r[0]
                tz = asz - r[2]
                key = (int(round(tx)), int(round(ty)), int(round(tz)), rot_name)
                if key in seen:
                    continue
                matches = 0
                for rr in rotated:
                    wx = rr[0] + tx
                    wz = rr[2] + tz
                    if (int(round(wx)), int(round(wz))) in a_stud_xz:
                        matches += 1
                if matches >= min_studs_matched:
                    seen.add(key)
                    placements.append(Placement(
                        x=tx, y=ty, z=tz,
                        rotation=rot_name,
                        studs_matched=matches,
                        direction="B-on-A",
                    ))
    return placements


def _distinct_xz(placements: list[Placement]) -> int:
    """Count physically-distinct outcomes — collapses rotation-symmetric duplicates."""
    return len({(round(p.x, 1), round(p.z, 1), round(p.y, 1)) for p in placements})


def find_connections(a: Part, b: Part, *, full_nesting_only: bool = False) -> dict[str, Any]:
    """High-level summary: every way B can connect to A, in both directions.

    `total_ways` counts (translation, rotation) pairs. `distinct_outcomes`
    collapses placements where rotation doesn't change the visual result
    (e.g. a 2x2 brick on a 2x4 has 12 (T, rot) pairs but only 3 distinct
    physical outcomes, because 2x2 is symmetric under 90° rotation).

    By default we return all placements with >= 1 stud clutched (so
    running-bond half-overlap counts as connected). Set
    full_nesting_only=True to only return placements where ALL of B's
    receptors mate with A studs.
    """
    receptors_b = len(receptor_positions(b))
    receptors_a = len(receptor_positions(a))
    min_b = receptors_b if full_nesting_only else 1
    min_a = receptors_a if full_nesting_only else 1
    b_on_a = find_placements_b_on_a(a, b, min_studs_matched=min_b)
    a_on_b = []
    for p in find_placements_b_on_a(b, a, min_studs_matched=min_a):
        a_on_b.append(Placement(
            x=-p.x, y=-p.y, z=-p.z,
            rotation=p.rotation, studs_matched=p.studs_matched,
            direction="A-on-B",
        ))
    return {
        "part_a": a.part_id,
        "part_b": b.part_id,
        "a_top_studs": len(a.studs),
        "b_top_studs": len(b.studs),
        "b_on_a_placements": [_p_dict(p) for p in b_on_a],
        "a_on_b_placements": [_p_dict(p) for p in a_on_b],
        "b_on_a_distinct": _distinct_xz(b_on_a),
        "a_on_b_distinct": _distinct_xz(a_on_b),
        "total_ways": len(b_on_a) + len(a_on_b),
        "distinct_outcomes": _distinct_xz(b_on_a) + _distinct_xz(a_on_b),
    }


def _p_dict(p: Placement) -> dict[str, Any]:
    return {"x": p.x, "y": p.y, "z": p.z, "rotation": p.rotation,
            "studs_matched": p.studs_matched, "direction": p.direction}
