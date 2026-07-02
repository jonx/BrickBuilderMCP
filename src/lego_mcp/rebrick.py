"""Re-brick 1x1 voxel constructions into bonded standard brickwork.

Voxel-style generation (mosaics, sculptures, imported heightmaps) emits 1x1
bricks. A 1x1 mates with exactly one brick below and one above, so it can only
form a chain — adjacent columns never bond and the "model" is really hundreds
of separate towers (see connection_graph.structural_analysis).

This module re-tiles each horizontal layer with real bricks of the same
color, scoring candidate placements by how many seams of the layer below they
CROSS — running bond in both axes. Same shape, same colors, one rigid body,
and typically a third of the part count.

Pure module: no server import. Operates on instance records exposing
part_id/color/x/y/z/rotation and returns new placement specs.
"""

from __future__ import annotations

from typing import Any, Iterable

STUD = 20.0
BRICK_H = 24.0
GRID_TOL = 0.5

# (length_studs, width_studs) -> builtin part id, tried largest-area first.
BRICK_CATALOG: tuple[tuple[int, int, str], ...] = (
    (10, 2, "3006"),
    (6, 2, "2456"),
    (4, 2, "3001"),
    (8, 1, "3008"),
    (3, 2, "3002"),
    (6, 1, "3009"),
    (2, 2, "3003"),
    (4, 1, "3010"),
    (3, 1, "3622"),
    (2, 1, "3004"),
    (1, 1, "3005"),
)

# Scoring weights: crossing a below-seam is what creates bond; resting on
# several distinct bricks adds redundancy; area breaks ties toward fewer,
# larger parts.
W_SEAM = 4.0
W_SUPPORT = 2.0
W_AREA = 1.0
W_ORIENT = 0.5   # nudge toward the level's preferred orientation (lamination)
# Penalty for ending a brick exactly where the layer below has a seam: a
# greedy tiler anchored at a run start can never CROSS the first below-seam
# (that would need a longer brick), but it can avoid REPEATING it — the next
# brick then starts mid-span and crosses it. This is what makes running bond
# emerge instead of phase-locked identical layers stacking into columns.
W_ALIGN = 3.0


def _snap(value: float, unit: float) -> int | None:
    q = round(value / unit)
    return int(q) if abs(value - q * unit) <= GRID_TOL else None


def extract_cells(instances: Iterable[Any]
                  ) -> tuple[dict[int, dict[tuple[int, int, int], int]], list[Any]]:
    """Split instances into re-brickable 1x1 voxel cells and passthrough parts.

    A cell is a 1x1 brick (3005) sitting on the half-stud XZ grid at a
    quarter-plate height. Cells are partitioned by their vertical lattice
    residue (height mod 24 LDU) so stacks on the ground (y=0, -24, ...) and
    stacks on a baseplate (y=-4, -28, ...) are each consolidated on their own
    lattice. Everything else passes through untouched.

    Returns ({residue: {(gx, level, gz): color_id}}, passthrough_instances).
    """
    lattices: dict[tuple[int, int, int], dict[tuple[int, int, int], int]] = {}
    passthrough: list[Any] = []
    for inst in instances:
        if inst.part_id != "3005":
            passthrough.append(inst)
            continue
        hx = _snap(inst.x, 10.0)          # half-stud quantum
        hz = _snap(inst.z, 10.0)
        h4 = _snap(-inst.y, 4.0)          # quarter-plate quantum
        if hx is None or hz is None or h4 is None:
            passthrough.append(inst)
            continue
        rx, rz = hx % 2, hz % 2           # 0 = even-10 grid, 1 = baseplate grid
        ry = (h4 * 4) % int(BRICK_H)
        key = (rx * 10, rz * 10, ry)
        cell = ((hx - rx) // 2, (h4 * 4 - ry) // int(BRICK_H), (hz - rz) // 2)
        lattices.setdefault(key, {})[cell] = int(inst.color)
    return lattices, passthrough


def _tile_layer(layer: dict[tuple[int, int], int],
                below_owner: dict[tuple[int, int], int],
                level: int,
                ) -> list[dict[str, Any]]:
    """Tile one layer's colored cells with bricks. Returns placements, each
    {"part_id", "color", "cells": [(gx, gz), ...], "rotation"}."""
    remaining = dict(layer)
    placements: list[dict[str, Any]] = []
    prefer_x = (level % 2 == 0)

    # Raster scan with min-corner anchors. Phase variation between layers
    # comes from the W_ALIGN penalty (never end a brick on a below-seam),
    # which is what makes running bond emerge.
    xs = sorted({gx for gx, _ in remaining})
    zs = sorted({gz for _, gz in remaining})
    scan = [(gx, gz) for gz in zs for gx in xs]

    owner_seq = 0
    for anchor in scan:
        if anchor not in remaining:
            continue
        ax, az = anchor
        color = remaining[anchor]
        best = None
        for length, width, pid in BRICK_CATALOG:
            orients = [(length, width, "identity")]
            if length != width:
                orients.append((width, length, "rot90y"))
            for dx, dz, rot in orients:
                # Anchor at min corner; scanning direction handles phase.
                cell_list = [(ax + i, az + j) for i in range(dx) for j in range(dz)]
                if any(remaining.get(c) != color for c in cell_list):
                    continue
                supports = {below_owner[c] for c in cell_list if c in below_owner}
                # A below-seam is CROSSED when the brick covers both of two
                # adjacent cells whose supporters differ.
                covered = set(cell_list)
                seams = 0
                for (cx, cz) in cell_list:
                    for nb in ((cx + 1, cz), (cx, cz + 1)):
                        if nb not in covered:
                            continue
                        oa, ob = below_owner.get((cx, cz)), below_owner.get(nb)
                        if oa is not None and ob is not None and oa != ob:
                            seams += 1
                # Aligned seams: the brick's own boundary sits exactly on a
                # boundary of the layer below (with layer material on the
                # other side, so a continuous 2-layer seam would form).
                aligned = 0
                for (cx, cz) in cell_list:
                    for nb in ((cx + 1, cz), (cx - 1, cz),
                               (cx, cz + 1), (cx, cz - 1)):
                        if nb in covered or nb not in layer:
                            continue
                        oa, ob = below_owner.get((cx, cz)), below_owner.get(nb)
                        if oa is not None and ob is not None and oa != ob:
                            aligned += 1
                score = (W_SEAM * seams + W_SUPPORT * len(supports)
                         + W_AREA * dx * dz - W_ALIGN * aligned)
                if (rot == "identity") == prefer_x and length != width:
                    score += W_ORIENT
                if best is None or score > best[0]:
                    best = (score, pid, cell_list, rot)
        _, pid, cell_list, rot = best
        owner_seq += 1
        placements.append({"part_id": pid, "color": color,
                           "cells": cell_list, "rotation": rot,
                           "owner": owner_seq})
        for c in cell_list:
            del remaining[c]
    return placements


def rebrick_cells(cells: dict[tuple[int, int, int], int],
                  x_offset_ldu: int = 0,
                  z_offset_ldu: int = 0,
                  y_offset_ldu: int = 0) -> list[dict[str, Any]]:
    """Consolidate one lattice of voxel cells bottom-up. Returns placement
    specs with world coords: {"part_id", "color", "x", "y", "z", "rotation"}."""
    by_level: dict[int, dict[tuple[int, int], int]] = {}
    for (gx, level, gz), color in cells.items():
        by_level.setdefault(level, {})[(gx, gz)] = color

    specs: list[dict[str, Any]] = []
    below_owner: dict[tuple[int, int], int] = {}
    for level in sorted(by_level):
        placements = _tile_layer(by_level[level], below_owner, level)
        below_owner = {}
        for p in placements:
            xs = [c[0] for c in p["cells"]]
            zs = [c[1] for c in p["cells"]]
            specs.append({
                "part_id": p["part_id"],
                "color": p["color"],
                "x": (min(xs) + max(xs)) / 2 * STUD + x_offset_ldu,
                "y": -(y_offset_ldu + level * BRICK_H),
                "z": (min(zs) + max(zs)) / 2 * STUD + z_offset_ldu,
                "rotation": p["rotation"],
            })
            for c in p["cells"]:
                below_owner[c] = p["owner"]
    return specs


def rebrick_instances(instances: Iterable[Any]
                      ) -> tuple[list[dict[str, Any]], list[Any], dict[str, int]]:
    """Full pipeline: split, consolidate each vertical lattice, report.

    Returns (new_specs, passthrough_instances, stats).
    """
    lattices, passthrough = extract_cells(instances)
    specs: list[dict[str, Any]] = []
    cells_in = 0
    for (rx, rz, ry), cells in sorted(lattices.items()):
        cells_in += len(cells)
        specs.extend(rebrick_cells(cells, x_offset_ldu=rx,
                                   z_offset_ldu=rz, y_offset_ldu=ry))
    stats = {
        "cells_in": cells_in,
        "bricks_out": len(specs),
        "passthrough": len(passthrough),
    }
    return specs, passthrough, stats
