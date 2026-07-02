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
    studs: int = 1   # how many stud-receiver pairs mate between a and b


def _complementary(t1: ConnectorType, t2: ConnectorType) -> bool:
    """Phase 1: only stud-top mates with bottom receiver."""
    return ({t1, t2} == {ConnectorType.STUD_TOP, ConnectorType.STUD_RECEIVER_BOTTOM})


def collect_world_connectors(parts) -> dict[str, list[WorldConnector]]:
    """For every known part instance, project its connectors to world coords.
    Definitions are derived from real catalog geometry (see
    `connectors.definition_for`); only unknown part_ids are excluded."""
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

    pair_counts: dict[tuple[str, str], int] = {}
    pair_types: dict[tuple[str, str], tuple[ConnectorType, ConnectorType]] = {}
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
                if a.instance_id < b.instance_id:
                    pair = (a.instance_id, b.instance_id)
                    types = (a.type, b.type)
                else:
                    pair = (b.instance_id, a.instance_id)
                    types = (b.type, a.type)
                pair_counts[pair] = pair_counts.get(pair, 0) + 1
                pair_types.setdefault(pair, types)
    return [ConnectionEdge(a=p[0], b=p[1],
                           type_a=pair_types[p][0], type_b=pair_types[p][1],
                           studs=n)
            for p, n in pair_counts.items()]


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
    """A part is an anchor (grounded) iff its bottom face rests on the table
    (world y ~ 0). Everything else must reach an anchor through real
    stud-receptor edges — in either direction: stacking on top of a grounded
    chain and hanging underneath one are both legitimate.

    Note there is deliberately NO "sits on a grounded part's top face" AABB
    fallback here: resting on a brick without stud mating (e.g. half a stud
    off) is not a LEGO connection and must be reported, not blessed.
    """
    from lego_mcp.server import PART_INDEX, part_aabb_world
    anchors: set[str] = set()
    for inst in parts.values():
        part = PART_INDEX.get(inst.part_id)
        if part is not None:
            bottom_y = part_aabb_world(inst, part)[1][1]   # max y == bottom (-Y is up)
        else:
            bottom_y = inst.y
        if abs(bottom_y) < GROUND_Y_TOL:
            anchors.add(inst.instance_id)
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
# Structural cohesion: is this ONE interlocked body, or a pile?
# ---------------------------------------------------------------------------
# Connectivity-to-ground (floating/unanchored) proves each part is *placeable*.
# It does not prove the model holds together: a forest of independent 1x1
# columns standing side by side passes every per-part check while being 2000
# separate objects. These metrics capture cohesion.

def _articulation_points(graph: dict[str, set[str]]) -> set[str]:
    """Parts whose removal splits their component (single points of failure).
    Iterative Tarjan — recursion-free so 20k-part models don't blow the stack."""
    visited: set[str] = set()
    disc: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str] = {}
    aps: set[str] = set()
    timer = 0
    for root in graph:
        if root in visited:
            continue
        root_children = 0
        visited.add(root)
        disc[root] = low[root] = timer
        timer += 1
        stack: list[tuple[str, Iterable[str]]] = [(root, iter(graph[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nb in it:
                if nb == parent.get(node):
                    continue
                if nb in visited:
                    low[node] = min(low[node], disc[nb])
                else:
                    parent[nb] = node
                    visited.add(nb)
                    disc[nb] = low[nb] = timer
                    timer += 1
                    stack.append((nb, iter(graph[nb])))
                    advanced = True
                    break
            if not advanced:
                stack.pop()
                p = parent.get(node)
                if p is None:
                    continue
                low[p] = min(low[p], low[node])
                if p == root:
                    root_children += 1
                elif low[node] >= disc[p]:
                    aps.add(p)
        if root_children >= 2:
            aps.add(root)
    return aps


def structural_analysis(parts,
                        graph: dict[str, set[str]] | None = None,
                        edges: list[ConnectionEdge] | None = None,
                        ) -> dict:
    """Cohesion report for the model.

    - rigid_bodies: connected components of the stud graph. A build should be
      ONE. Two towers that merely stand near each other are two.
    - largest_body / fragment sample: to locate the pieces.
    - single_stud_joints: non-grounded parts whose TOTAL stud engagement is 1
      — they pivot/pop off (hinge risk).
    - articulation_parts: parts whose removal splits the structure; their
      ratio is high in chain-like builds and low in well-bonded ones.
    - spanning_ratio: fraction of elevated parts resting on >= 2 distinct
      parts below — the masonry-bond measure (1x1 columns score 0).
    """
    if graph is None or edges is None:
        graph, edges = build_graph(parts)

    # Connected components, largest first.
    seen: set[str] = set()
    components: list[list[str]] = []
    for pid in graph:
        if pid in seen:
            continue
        comp = [pid]
        seen.add(pid)
        stack = [pid]
        while stack:
            cur = stack.pop()
            for nb in graph[cur]:
                if nb not in seen:
                    seen.add(nb)
                    comp.append(nb)
                    stack.append(nb)
        components.append(comp)
    components.sort(key=len, reverse=True)

    engagement: dict[str, int] = defaultdict(int)
    for e in edges:
        engagement[e.a] += e.studs
        engagement[e.b] += e.studs
    anchors = find_anchors(parts)

    single_stud = [pid for pid in graph
                   if pid not in anchors and engagement.get(pid, 0) == 1]

    aps = _articulation_points(graph)

    elevated = [pid for pid in graph if pid not in anchors]
    spanning = 0
    for pid in elevated:
        below = [n for n in graph[pid] if parts[n].y > parts[pid].y]
        if len(below) >= 2:
            spanning += 1
    spanning_ratio = (spanning / len(elevated)) if elevated else 1.0

    return {
        "rigid_bodies": len(components),
        "largest_body": len(components[0]) if components else 0,
        "fragment_sample": [c[0] for c in components[1:6]],
        "single_stud_joints": len(single_stud),
        "single_stud_sample": sorted(single_stud)[:5],
        "articulation_parts": len(aps),
        "articulation_ratio": round(len(aps) / len(graph), 3) if graph else 0.0,
        "spanning_ratio": round(spanning_ratio, 3),
    }


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


def _run_axis_and_endpoints(inst) -> tuple[str, int, int] | None:
    """Return the horizontal run axis and world endpoints for a rectangular brick.

    Rotation changes whether a 1x4/2x4 contributes seams along X or Z. Square
    parts are ambiguous, so we skip them for seam scoring; they are usually
    fillers or corner pieces and otherwise add more noise than signal.
    """
    from lego_mcp.server import PART_INDEX, part_aabb_world
    part = PART_INDEX.get(inst.part_id)
    if part is None:
        return None
    (xmin, _ymin, zmin), (xmax, _ymax, zmax) = part_aabb_world(inst, part)
    x_len = xmax - xmin
    z_len = zmax - zmin
    if abs(x_len - z_len) < 0.5:
        return None
    if x_len > z_len:
        return ("x", int(round(xmin)), int(round(xmax)))
    return ("z", int(round(zmin)), int(round(zmax)))


def vertical_seam_score(parts, subassembly: str | None = None) -> int:
    """Count adjacent-row pairs that share an INTERNAL seam X position (a
    continuous vertical seam between bricks). Lower is better; 0 = perfect stagger.

    Wall-end positions (the row's min / max) are not counted — they're the
    wall boundary, not a between-brick seam.
    """
    rows: dict[int, dict[str, set[int]]] = defaultdict(lambda: {"x": set(), "z": set()})
    for inst in parts.values():
        if subassembly is not None and inst.subassembly != subassembly:
            continue
        run = _run_axis_and_endpoints(inst)
        if run is None:
            continue
        axis, start, end = run
        row = _row_of(inst)
        rows[row][axis].add(start)
        rows[row][axis].add(end)
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
    rows_x: dict[int, set[int]] = defaultdict(set)
    rows_z: dict[int, set[int]] = defaultdict(set)
    for inst in parts.values():
        if subassembly is not None and inst.subassembly != subassembly:
            continue
        run = _run_axis_and_endpoints(inst)
        if run is None:
            continue
        axis, start, end = run
        row = _row_of(inst)
        if axis == "x":
            rows_x[row].add(start)
            rows_x[row].add(end)
        else:
            rows_z[row].add(start)
            rows_z[row].add(end)
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
