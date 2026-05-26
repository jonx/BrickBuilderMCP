"""Assembly-level connection ports.

This module lifts the part-level connector model into group semantics:
which studs/receivers of a whole subassembly are still exposed, and where
another subassembly could connect to them.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from lego_mcp.connection_graph import CONNECTION_TOL, collect_world_connectors
from lego_mcp.connectors import ConnectorType, WorldConnector

STUD = 20.0


@dataclass(frozen=True)
class ConnectorRef:
    instance_id: str
    local_index: int
    type: ConnectorType
    x: float
    y: float
    z: float


def _complementary(t1: ConnectorType, t2: ConnectorType) -> bool:
    return ({t1, t2} == {ConnectorType.STUD_TOP, ConnectorType.STUD_RECEIVER_BOTTOM})


def _connector_key(c: WorldConnector) -> tuple[str, int]:
    return (c.instance_id, c.local_index)


def _bucket_key(c: WorldConnector) -> tuple[int, int, int]:
    return (
        int(round(c.x / CONNECTION_TOL)),
        int(round(c.y / CONNECTION_TOL)),
        int(round(c.z / CONNECTION_TOL)),
    )


def _internal_mated_connectors(world_by_id: dict[str, list[WorldConnector]]) -> set[tuple[str, int]]:
    """Return connector keys already consumed by mates within this assembly."""
    bucket: dict[tuple[int, int, int], list[WorldConnector]] = defaultdict(list)
    for conns in world_by_id.values():
        for conn in conns:
            bucket[_bucket_key(conn)].append(conn)

    used: set[tuple[str, int]] = set()
    for conns in bucket.values():
        if len(conns) < 2:
            continue
        for i, a in enumerate(conns):
            for b in conns[i + 1:]:
                if a.instance_id == b.instance_id:
                    continue
                if _complementary(a.type, b.type):
                    used.add(_connector_key(a))
                    used.add(_connector_key(b))
    return used


def exposed_connectors(parts: dict[str, Any]) -> list[ConnectorRef]:
    """All top studs / bottom receivers not mated inside `parts`."""
    world_by_id = collect_world_connectors(parts)
    used = _internal_mated_connectors(world_by_id)
    exposed: list[ConnectorRef] = []
    for conns in world_by_id.values():
        for c in conns:
            if _connector_key(c) in used:
                continue
            exposed.append(ConnectorRef(
                instance_id=c.instance_id,
                local_index=c.local_index,
                type=c.type,
                x=c.x, y=c.y, z=c.z,
            ))
    return exposed


def _plane_key(c: ConnectorRef) -> tuple[ConnectorType, int]:
    return (c.type, int(round(c.y / CONNECTION_TOL)))


def _grid_key(c: ConnectorRef) -> tuple[int, int]:
    return (int(round(c.x)), int(round(c.z)))


def _cluster_connectors(connectors: list[ConnectorRef]) -> list[list[ConnectorRef]]:
    """Cluster exposed connectors into same-plane contiguous stud regions."""
    by_plane: dict[tuple[ConnectorType, int], dict[tuple[int, int], ConnectorRef]] = defaultdict(dict)
    for c in connectors:
        by_plane[_plane_key(c)][_grid_key(c)] = c

    clusters: list[list[ConnectorRef]] = []
    for grid in by_plane.values():
        unseen = set(grid.keys())
        while unseen:
            start = unseen.pop()
            queue: deque[tuple[int, int]] = deque([start])
            cluster_keys = [start]
            while queue:
                x, z = queue.popleft()
                for n in ((x + int(STUD), z), (x - int(STUD), z),
                          (x, z + int(STUD)), (x, z - int(STUD))):
                    if n in unseen:
                        unseen.remove(n)
                        queue.append(n)
                        cluster_keys.append(n)
            clusters.append([grid[k] for k in cluster_keys])
    return clusters


def _port_dict(port_id: str, cluster: list[ConnectorRef]) -> dict[str, Any]:
    xs = [c.x for c in cluster]
    ys = [c.y for c in cluster]
    zs = [c.z for c in cluster]
    ctype = cluster[0].type
    direction = "up" if ctype == ConnectorType.STUD_TOP else "down"
    return {
        "id": port_id,
        "type": ctype.value,
        "direction": direction,
        "connectors": len(cluster),
        "center": [
            round(sum(xs) / len(xs), 3),
            round(sum(ys) / len(ys), 3),
            round(sum(zs) / len(zs), 3),
        ],
        "bounds": {
            "x": [round(min(xs), 3), round(max(xs), 3)],
            "y": [round(min(ys), 3), round(max(ys), 3)],
            "z": [round(min(zs), 3), round(max(zs), 3)],
        },
        "sample_connectors": [
            {
                "instance_id": c.instance_id,
                "local_index": c.local_index,
                "x": round(c.x, 3),
                "y": round(c.y, 3),
                "z": round(c.z, 3),
            }
            for c in sorted(cluster, key=lambda c: (c.x, c.z, c.instance_id))[:10]
        ],
    }


def analyze_ports(parts: dict[str, Any], max_connectors: int = 200,
                  max_ports: int = 50) -> dict[str, Any]:
    exposed = exposed_connectors(parts)
    clusters = _cluster_connectors(exposed)
    clusters.sort(key=len, reverse=True)

    counts: dict[str, int] = defaultdict(int)
    for c in exposed:
        counts[c.type.value] += 1

    return {
        "parts": len(parts),
        "exposed_connector_count": len(exposed),
        "counts_by_type": dict(sorted(counts.items())),
        "ports_total": len(clusters),
        "ports": [
            _port_dict(f"port_{i + 1}", cluster)
            for i, cluster in enumerate(clusters[:max_ports])
        ],
        "connectors_truncated": len(exposed) > max_connectors,
        "connectors": [
            {
                "instance_id": c.instance_id,
                "local_index": c.local_index,
                "type": c.type.value,
                "x": round(c.x, 3),
                "y": round(c.y, 3),
                "z": round(c.z, 3),
            }
            for c in sorted(exposed, key=lambda c: (c.y, c.type.value, c.x, c.z))[:max_connectors]
        ],
    }


def connection_offsets(movable_parts: dict[str, Any], target_parts: dict[str, Any],
                       limit: int = 20) -> list[dict[str, Any]]:
    """Candidate offsets that align exposed connectors between two assemblies."""
    movable = exposed_connectors(movable_parts)
    target = exposed_connectors(target_parts)
    by_offset: dict[tuple[int, int, int], dict[str, Any]] = {}

    for src in movable:
        for dst in target:
            if not _complementary(src.type, dst.type):
                continue
            key = (
                int(round(dst.x - src.x)),
                int(round(dst.y - src.y)),
                int(round(dst.z - src.z)),
            )
            entry = by_offset.setdefault(key, {
                "offset": [float(key[0]), float(key[1]), float(key[2])],
                "connections": 0,
                "source_type": src.type.value,
                "target_type": dst.type.value,
                "sample_pairs": [],
            })
            entry["connections"] += 1
            if len(entry["sample_pairs"]) < 8:
                entry["sample_pairs"].append({
                    "source": {
                        "instance_id": src.instance_id,
                        "local_index": src.local_index,
                        "type": src.type.value,
                    },
                    "target": {
                        "instance_id": dst.instance_id,
                        "local_index": dst.local_index,
                        "type": dst.type.value,
                    },
                })

    return sorted(
        by_offset.values(),
        key=lambda item: (
            -item["connections"],
            abs(item["offset"][0]) + abs(item["offset"][1]) + abs(item["offset"][2]),
        ),
    )[:limit]
