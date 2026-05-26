"""Connection graph: parts as nodes, mating connectors as edges.

Built fresh on each validate_model call. Used to:
- detect floating parts (no connectors mate with any neighbor and not grounded)
- detect unanchored islands (BFS from grounded; anything unreached is floating)
- compute wall-bonding diagnostics (seam-score, bond-quality)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from lego_mcp.connectors import (
    ConnectorType,
    PartDefinition,
    WorldConnector,
    definition_for,
    world_connectors,
)

CONNECTION_TOL = 0.5  # LDU — XZ + Y position-match tolerance for two connectors
GROUND_Y_TOL = 0.5    # LDU — how close to y=0 counts as grounded


@dataclass(frozen=True)
class ConnectionEdge:
    a: str
    b: str
    type_a: ConnectorType
    type_b: ConnectorType


def _complementary(t1: ConnectorType, t2: ConnectorType) -> bool:
    """Phase 1: only stud-top mates with bottom receiver."""
    return ({t1, t2} == {ConnectorType.STUD_TOP, ConnectorType.STUD_RECEIVER_BOTTOM})


def collect_world_connectors(parts) -> dict[str, list[WorldConnector]]:
    """For every part instance with a supported definition, project its
    connectors to world coords. Parts without a PartDefinition (e.g. tiles,
    slopes, baseplates) are excluded — those go through the AABB path."""
    out: dict[str, list[WorldConnector]] = {}
    for inst in parts.values():
        defn = definition_for(inst.part_id)
        if defn is None:
            continue
        out[inst.instance_id] = world_connectors(
            inst.instance_id, defn, inst.x, inst.y, inst.z, inst.rotation,
        )
    return out


def _quantize_xz(x: float, z: float) -> tuple[int, int]:
    return (int(round(x / CONNECTION_TOL)), int(round(z / CONNECTION_TOL)))


def find_edges(world_by_id: dict[str, list[WorldConnector]]) -> list[ConnectionEdge]:
    """Find every pair of connectors from DIFFERENT parts that mate.

    A mating pair has:
      - complementary types (STUD_TOP ↔ STUD_RECEIVER_BOTTOM)
      - matching world X and Z within CONNECTION_TOL
      - matching world Y within CONNECTION_TOL (top stud and bottom receiver
        resolve to the SAME world Y at the mating plane)

    Returns one edge per mating pair. Note: two parts may share many edges
    (one per stud) but we de-duplicate at the (a, b) level for graph purposes.
    """
    # Bucket connectors by (qx, qy, qz) so we only test pairs that share a cell.
    bucket: dict[tuple[int, int, int], list[WorldConnector]] = defaultdict(list)
    for wcs in world_by_id.values():
        for wc in wcs:
            key = (int(round(wc.x / CONNECTION_TOL)),
                   int(round(wc.y / CONNECTION_TOL)),
                   int(round(wc.z / CONNECTION_TOL)))
            bucket[key].append(wc)

    edges: list[ConnectionEdge] = []
    seen_pairs: set[tuple[str, str]] = set()
    for cell, wcs in bucket.items():
        if len(wcs) < 2:
            continue
        for i in range(len(wcs)):
            for j in range(i + 1, len(wcs)):
                a, b = wcs[i], wcs[j]
                if a.instance_id == b.instance_id:
                    continue
                if not _complementary(a.type, b.type):
                    continue
                pair = (a.instance_id, b.instance_id)
                pair = pair if pair[0] < pair[1] else (pair[1], pair[0])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                edges.append(ConnectionEdge(
                    a=pair[0], b=pair[1],
                    type_a=a.type, type_b=b.type,
                ))
    return edges


def build_graph(parts) -> tuple[dict[str, set[str]], list[ConnectionEdge]]:
    """Build the part-to-part adjacency graph. Returns (neighbors, edges)."""
    world_by_id = collect_world_connectors(parts)
    edges = find_edges(world_by_id)
    graph: dict[str, set[str]] = {pid: set() for pid in parts}
    for e in edges:
        graph[e.a].add(e.b)
        graph[e.b].add(e.a)
    return graph, edges


def find_anchors(parts) -> set[str]:
    """A part is an anchor (grounded) if:
    - its bottom face is at y=0 (sitting on the table), OR
    - it sits on top of a part that IS itself grounded AND whose footprint
      covers the part's XZ position. The typical case: a brick at y=-4 over
      a baseplate at y=0. (We approximate "footprint covers" by AABB XZ.)
    """
    from lego_mcp.server import PART_INDEX, part_aabb_world
    grounded_anchors: list[tuple[str, tuple]] = []   # (id, AABB)
    anchors: set[str] = set()

    # Pass 1: direct ground contact (y ~ 0).
    for inst in parts.values():
        if abs(inst.y) < GROUND_Y_TOL:
            anchors.add(inst.instance_id)
            part = PART_INDEX.get(inst.part_id)
            if part is not None:
                grounded_anchors.append((inst.instance_id, part_aabb_world(inst, part)))

    # Pass 2: parts sitting on the top face of any directly-grounded part.
    for inst in parts.values():
        if inst.instance_id in anchors:
            continue
        part = PART_INDEX.get(inst.part_id)
        if part is None:
            continue
        ab = part_aabb_world(inst, part)
        bottom_y = ab[1][1]
        for (aid, anchor_ab) in grounded_anchors:
            anchor_top_y = anchor_ab[0][1]      # min y == top in our convention
            if abs(anchor_top_y - bottom_y) > GROUND_Y_TOL:
                continue
            # XZ overlap > 0 means the part stands on this grounded anchor.
            dx = min(ab[1][0], anchor_ab[1][0]) - max(ab[0][0], anchor_ab[0][0])
            dz = min(ab[1][2], anchor_ab[1][2]) - max(ab[0][2], anchor_ab[0][2])
            if dx > 0 and dz > 0:
                anchors.add(inst.instance_id)
                break
    return anchors


def find_floating_and_unanchored(parts) -> tuple[dict[str, set[str]],
                                                   list[ConnectionEdge],
                                                   set[str], set[str], set[str]]:
    """Return (graph, edges, anchors, floating_ids, unanchored_ids)."""
    graph, edges = build_graph(parts)
    anchors = find_anchors(parts)
    # BFS from anchors
    reachable: set[str] = set(anchors)
    queue = list(anchors)
    while queue:
        cur = queue.pop()
        for n in graph.get(cur, ()):
            if n not in reachable:
                reachable.add(n)
                queue.append(n)
    floating: set[str] = set()
    unanchored: set[str] = set()
    for pid in parts:
        if pid in reachable:
            continue
        if not graph[pid]:
            floating.add(pid)            # no neighbors at all
        else:
            unanchored.add(pid)          # has neighbors but island doesn't reach ground
    return graph, edges, anchors, floating, unanchored


# ---------------------------------------------------------------------------
# Wall-bonding diagnostics (per-subassembly metrics)
# ---------------------------------------------------------------------------

def _row_of(inst, brick_height: float = 24.0, baseplate_top: float = -4.0) -> int:
    """Brick-row index (0 = first row on baseplate). Negative because -Y is up."""
    return int(round((-inst.y - (-baseplate_top)) / brick_height))


def _internal_seams(endpoints: set[int]) -> set[int]:
    """Strip the row's min/max endpoints — those are the wall ends, not seams."""
    if len(endpoints) <= 2:
        return set()
    sorted_eps = sorted(endpoints)
    return set(sorted_eps[1:-1])


def vertical_seam_score(parts, subassembly: str | None = None) -> int:
    """Count adjacent-row pairs that share an INTERNAL seam X position (a
    continuous vertical seam between bricks). Lower is better; 0 = perfect stagger.

    Wall-end positions (the row's min / max) are not counted — they're the
    wall boundary, not a between-brick seam.
    """
    from lego_mcp.connectors import definition_for
    rows: dict[int, dict[str, set[int]]] = defaultdict(lambda: {"x": set(), "z": set()})
    for inst in parts.values():
        if subassembly is not None and inst.subassembly != subassembly:
            continue
        defn = definition_for(inst.part_id)
        if defn is None:
            continue
        row = _row_of(inst)
        if defn.width_studs >= defn.depth_studs:
            half = defn.width_ldu / 2
            rows[row]["x"].add(int(round(inst.x - half)))
            rows[row]["x"].add(int(round(inst.x + half)))
        else:
            half = defn.depth_ldu / 2
            rows[row]["z"].add(int(round(inst.z - half)))
            rows[row]["z"].add(int(round(inst.z + half)))
    sorted_rows = sorted(rows.keys())
    score = 0
    for i in range(len(sorted_rows) - 1):
        r1, r2 = sorted_rows[i], sorted_rows[i + 1]
        if r2 - r1 > 1:
            continue
        for axis in ("x", "z"):
            score += len(_internal_seams(rows[r1][axis])
                          & _internal_seams(rows[r2][axis]))
    return score


def wall_bond_quality(parts, subassembly: str | None = None) -> float:
    """0..1 score: 1.0 means every adjacent-row pair has its seams fully shifted
    away from the row below. Computed as 1 - (shared seams / total seam slots)."""
    from lego_mcp.connectors import definition_for
    rows_x: dict[int, set[int]] = defaultdict(set)
    rows_z: dict[int, set[int]] = defaultdict(set)
    for inst in parts.values():
        if subassembly is not None and inst.subassembly != subassembly:
            continue
        defn = definition_for(inst.part_id)
        if defn is None:
            continue
        row = _row_of(inst)
        if defn.width_studs >= defn.depth_studs:
            half = defn.width_ldu / 2
            rows_x[row].add(int(round(inst.x - half)))
            rows_x[row].add(int(round(inst.x + half)))
        else:
            half = defn.depth_ldu / 2
            rows_z[row].add(int(round(inst.z - half)))
            rows_z[row].add(int(round(inst.z + half)))
    shared = 0
    total = 0
    for rows in (rows_x, rows_z):
        sorted_rows = sorted(rows.keys())
        for i in range(len(sorted_rows) - 1):
            r1, r2 = sorted_rows[i], sorted_rows[i + 1]
            if r2 - r1 > 1:
                continue
            int1, int2 = _internal_seams(rows[r1]), _internal_seams(rows[r2])
            shared += len(int1 & int2)
            total += len(int1 | int2)
    if total == 0:
        return 1.0
    return 1.0 - (shared / total)
