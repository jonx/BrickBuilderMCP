"""Human-style build sequencing for a completed LEGO model.

The high-level helpers compile geometry into a flat set of parts. This module
turns that flat target into an order a builder could follow: ground/baseplate
parts first, then any part whose bottom face is supported by already-built
parts.
"""

from __future__ import annotations

from typing import Any, Callable

SUPPORT_TOL = 0.5
MIN_SUPPORT_AREA = 400.0


def _xz_overlap_area(a: tuple, b: tuple) -> float:
    dx = min(a[1][0], b[1][0]) - max(a[0][0], b[0][0])
    dz = min(a[1][2], b[1][2]) - max(a[0][2], b[0][2])
    return max(0.0, dx) * max(0.0, dz)


def _top_y(aabb: tuple) -> float:
    # -Y is up, so the top face is the minimum Y coordinate.
    return aabb[0][1]


def _bottom_y(aabb: tuple) -> float:
    return aabb[1][1]


def _supporters_for(iid: str, aabbs: dict[str, tuple]) -> list[dict[str, Any]]:
    cand = aabbs[iid]
    bottom = _bottom_y(cand)
    supports = []
    for other_id, other in aabbs.items():
        if other_id == iid:
            continue
        if abs(_top_y(other) - bottom) > SUPPORT_TOL:
            continue
        area = _xz_overlap_area(cand, other)
        if area <= 0:
            continue
        supports.append({"instance_id": other_id, "area": area})
    supports.sort(key=lambda s: (-s["area"], s["instance_id"]))
    return supports


def _step_payload(step: int, inst: Any, aabb: tuple,
                  supports: list[dict[str, Any]]) -> dict[str, Any]:
    support_area = round(sum(s["area"] for s in supports), 2)
    return {
        "step": step,
        "instance_id": inst.instance_id,
        "part_id": inst.part_id,
        "color": inst.color,
        "position": [inst.x, inst.y, inst.z],
        "rotation": inst.rotation,
        "subassembly": inst.subassembly,
        "bottom_y": _bottom_y(aabb),
        "supports": supports,
        "support_area": support_area,
        "instruction": (
            f"Place {inst.part_id} at ({inst.x:g}, {inst.y:g}, {inst.z:g}) "
            f"rotation={inst.rotation}"
        ),
    }


def plan_build_sequence(parts: dict[str, Any],
                        part_index: dict[str, Any],
                        part_aabb_world: Callable[[Any, Any], tuple],
                        max_steps: int | None = None,
                        start_after: int = 0,
                        ) -> dict[str, Any]:
    """Return a support-respecting build sequence for `parts`.

    The planner is deterministic. It precomputes all possible below-support
    relationships, then repeatedly chooses the lowest available part whose
    supporters are already placed. Parts whose bottom face is at Y=0 are
    treated as ground-startable.
    """
    if start_after < 0:
        raise ValueError("start_after must be >= 0")
    if max_steps is not None and max_steps < 0:
        raise ValueError("max_steps must be >= 0")

    aabbs: dict[str, tuple] = {}
    unknown: list[str] = []
    for iid, inst in parts.items():
        part = part_index.get(inst.part_id)
        if part is None:
            unknown.append(iid)
            continue
        aabbs[iid] = part_aabb_world(inst, part)

    possible_supports = {
        iid: _supporters_for(iid, aabbs)
        for iid in aabbs
    }
    remaining = set(aabbs)
    built: set[str] = set()
    ordered: list[str] = []
    steps: list[dict[str, Any]] = []

    def ready_supports(iid: str) -> list[dict[str, Any]]:
        bottom = _bottom_y(aabbs[iid])
        if abs(bottom) <= SUPPORT_TOL:
            return [{"instance_id": "ground", "area": MIN_SUPPORT_AREA}]
        supports = [s for s in possible_supports[iid] if s["instance_id"] in built]
        if not supports:
            return []
        if any(s["area"] >= MIN_SUPPORT_AREA for s in supports):
            return supports
        if sum(s["area"] for s in supports) >= MIN_SUPPORT_AREA:
            return supports
        return []

    while remaining:
        candidates = [(iid, ready_supports(iid)) for iid in remaining]
        candidates = [(iid, supports) for iid, supports in candidates if supports]
        if not candidates:
            blocked = []
            for iid in sorted(remaining):
                inst = parts[iid]
                blocked.append({
                    "instance_id": iid,
                    "part_id": inst.part_id,
                    "position": [inst.x, inst.y, inst.z],
                    "rotation": inst.rotation,
                    "bottom_y": _bottom_y(aabbs[iid]),
                    "possible_supports": possible_supports[iid][:8],
                })
            return {
                "ok": False,
                "total_parts": len(parts),
                "sequenced": len(ordered),
                "unknown_parts": unknown,
                "blocked_count": len(blocked),
                "blocked": blocked[:50],
                "steps": steps[start_after:(
                    None if max_steps is None else start_after + max_steps
                )],
            }

        candidates.sort(key=lambda item: (
            -_bottom_y(aabbs[item[0]]),
            parts[item[0]].subassembly,
            parts[item[0]].z,
            parts[item[0]].x,
            parts[item[0]].part_id,
            item[0],
        ))
        iid, supports = candidates[0]
        built.add(iid)
        remaining.remove(iid)
        ordered.append(iid)
        steps.append(_step_payload(len(ordered), parts[iid], aabbs[iid], supports))

    window_end = None if max_steps is None else start_after + max_steps
    return {
        "ok": True,
        "total_parts": len(parts),
        "sequenced": len(ordered),
        "unknown_parts": unknown,
        "blocked_count": 0,
        "steps": steps[start_after:window_end],
        "start_after": start_after,
        "max_steps": max_steps,
    }


# ---------------------------------------------------------------------------
# Partial-vs-target sequencing: given a set of already-built instance_ids,
# find the next placeable unbuilt part.
# ---------------------------------------------------------------------------

def next_unbuilt_step(parts: dict[str, Any],
                       part_index: dict[str, Any],
                       part_aabb_world: Callable[[Any, Any], tuple],
                       built_set: set[str],
                       limit: int = 1,
                       ) -> dict[str, Any]:
    """Return the next `limit` unbuilt parts that are placeable given the
    current `built_set` (treated as the available supporters along with the
    ground plane).

    A part is "placeable now" if either:
      - its bottom face is on the ground (y ~ 0), OR
      - at least one of its possible supporters is in `built_set` AND the
        combined supported area is >= MIN_SUPPORT_AREA.

    Returns:
        {
            "ok": True/False,
            "built_count": int,
            "total_parts": int,
            "remaining": int,
            "candidates": [step-payloads],   # up to `limit`
            "blocked": [...],                # parts not yet placeable, with reasons
            "complete": bool,
        }
    """
    aabbs: dict[str, tuple] = {}
    unknown: list[str] = []
    for iid, inst in parts.items():
        part = part_index.get(inst.part_id)
        if part is None:
            unknown.append(iid)
            continue
        aabbs[iid] = part_aabb_world(inst, part)

    possible_supports = {
        iid: _supporters_for(iid, aabbs) for iid in aabbs
    }

    def is_placeable(iid: str) -> tuple[bool, list[dict[str, Any]]]:
        bottom = _bottom_y(aabbs[iid])
        if abs(bottom) <= SUPPORT_TOL:
            return True, [{"instance_id": "ground", "area": MIN_SUPPORT_AREA}]
        ready = [s for s in possible_supports[iid] if s["instance_id"] in built_set]
        if not ready:
            return False, []
        if any(s["area"] >= MIN_SUPPORT_AREA for s in ready):
            return True, ready
        if sum(s["area"] for s in ready) >= MIN_SUPPORT_AREA:
            return True, ready
        return False, ready  # partial — surface as info but not placeable yet

    unbuilt = [iid for iid in aabbs if iid not in built_set]
    placeable: list[tuple[str, list[dict[str, Any]]]] = []
    blocked: list[dict[str, Any]] = []
    for iid in unbuilt:
        ok, supports = is_placeable(iid)
        if ok:
            placeable.append((iid, supports))
        else:
            blocked.append({
                "instance_id": iid,
                "part_id": parts[iid].part_id,
                "position": [parts[iid].x, parts[iid].y, parts[iid].z],
                "needs_supporters_in_built": [s["instance_id"] for s in possible_supports[iid][:4]],
                "supporters_currently_in_built": [s["instance_id"] for s in possible_supports[iid] if s["instance_id"] in built_set],
            })

    # Sort placeable: bottom-up, then by subassembly, then by (z, x), then by id
    placeable.sort(key=lambda item: (
        -_bottom_y(aabbs[item[0]]),
        parts[item[0]].subassembly,
        parts[item[0]].z,
        parts[item[0]].x,
        parts[item[0]].part_id,
        item[0],
    ))
    chosen = placeable[:limit]
    candidates = [_step_payload(i + 1, parts[iid], aabbs[iid], supports)
                   for i, (iid, supports) in enumerate(chosen)]

    return {
        "ok": True,
        "built_count": len(built_set & set(aabbs)),
        "total_parts": len(parts),
        "remaining": len(unbuilt),
        "candidates": candidates,
        "blocked_count": len(blocked),
        "blocked": blocked[:20],
        "complete": len(unbuilt) == 0,
        "unknown_parts": unknown,
    }

