"""Reverse-mount lookup: 'what parts can mount on top of this part?'

Strategy:
- Each part's "receptor footprint" is its bottom-face stud-grid extent in
  studs (long axis, short axis). Derived from `connections.receptor_positions`
  via the part's dimensions.
- Index ALL parts by that footprint, so query time stays low even though the
  catalog has 23k+ parts. For a target A with stud-grid MxN, only parts
  whose footprint fits (≤MxN at any rotation) are candidates.
- The actual fit is verified with `find_placements_b_on_a`.

The index is built lazily on first query and cached in memory. ~1 second
build for the full LDraw library on this machine; queries return in milliseconds.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from lego_mcp.connections import find_placements_b_on_a, receptor_positions
from lego_mcp.parts import Part


_INDEX: dict[tuple[int, int], list[str]] | None = None


def _footprint_studs(p: Part) -> tuple[int, int] | None:
    """Bottom-receptor extent in studs as (long_axis, short_axis), or None
    if the part has no receptors (baseplate, tile-less parts)."""
    receptors = receptor_positions(p)
    if not receptors:
        return None
    xs = [r[0] for r in receptors]
    zs = [r[2] for r in receptors]
    w_studs = max(1, int(round((max(xs) - min(xs)) / 20)) + 1)
    d_studs = max(1, int(round((max(zs) - min(zs)) / 20)) + 1)
    long_, short_ = max(w_studs, d_studs), min(w_studs, d_studs)
    return long_, short_


def build_index(part_index: dict[str, Part]) -> dict[tuple[int, int], list[str]]:
    """Build the footprint → [part_id, ...] map. Idempotent + cached."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for pid, part in part_index.items():
        fp = _footprint_studs(part)
        if fp is None:
            continue
        buckets[fp].append(pid)
    _INDEX = dict(buckets)
    return _INDEX


def invalidate() -> None:
    """For tests / re-indexing."""
    global _INDEX
    _INDEX = None


def parts_that_mount_on(target: Part, part_index: dict[str, Part],
                         limit: int = 20,
                         min_studs_matched: int = 1,
                         ) -> list[dict[str, Any]]:
    """Return up to `limit` parts that can sit on top of `target` with at
    least one stud-receptor mating.

    Each result: {part_id, name, footprint_studs, placements_count, example_placement}.
    """
    if not target.studs:
        return []
    # Target's stud-grid extent — candidates can't be bigger than this in either axis.
    xs = [s[0] for s in target.studs]
    zs = [s[2] for s in target.studs]
    target_w = max(1, int(round((max(xs) - min(xs)) / 20)) + 1)
    target_d = max(1, int(round((max(zs) - min(zs)) / 20)) + 1)
    target_long, target_short = max(target_w, target_d), min(target_w, target_d)

    index = build_index(part_index)
    candidates: list[dict[str, Any]] = []
    # Iterate buckets that COULD fit (long ≤ target_long, short ≤ target_short)
    # PLUS swapped (the part may rotate 90deg to fit).
    for (b_long, b_short), pids in index.items():
        fits = (b_long <= target_long and b_short <= target_short) or \
               (b_short <= target_long and b_long <= target_short)
        if not fits:
            continue
        for pid in pids:
            cand = part_index[pid]
            placements = find_placements_b_on_a(target, cand,
                                                 min_studs_matched=min_studs_matched)
            if not placements:
                continue
            best = max(placements, key=lambda p: p.studs_matched)
            candidates.append({
                "part_id": pid,
                "name": cand.name.strip(),
                "footprint_studs": [b_long, b_short],
                "placements_count": len(placements),
                "best_match_studs": best.studs_matched,
                "example_placement": {
                    "x": best.x, "y": best.y, "z": best.z,
                    "rotation": best.rotation,
                    "studs_matched": best.studs_matched,
                },
            })
            if len(candidates) >= limit * 3:
                # Early exit if we've gathered enough; we'll trim & rank below.
                break
        if len(candidates) >= limit * 3:
            break

    # Rank: most studs matched first, then smallest part first (visual diversity).
    candidates.sort(key=lambda c: (-c["best_match_studs"],
                                    c["footprint_studs"][0] * c["footprint_studs"][1],
                                    c["part_id"]))
    return candidates[:limit]
