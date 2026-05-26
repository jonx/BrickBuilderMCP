"""Connection enumeration: real LEGO stud-mating combinatorics."""

from __future__ import annotations

import pytest

from lego_mcp import server
from lego_mcp.connections import find_connections, find_placements_b_on_a


@pytest.fixture(autouse=True)
def _ensure_lib():
    """Force the LDraw library to load so we have full stud catalogs."""
    server._ensure_library_loaded()


def test_2x2_fully_on_2x4_has_3_distinct_positions():
    """A 2x2 brick FULLY nests on a 2x4 brick at 3 distinct XZ positions
    (4 rotations each, all visually identical due to 2x2 symmetry)."""
    brick_2x4 = server.PART_INDEX["3001"]
    brick_2x2 = server.PART_INDEX["3003"]
    placements = find_placements_b_on_a(brick_2x4, brick_2x2, min_studs_matched=4)
    distinct_xz = {(round(p.x, 1), round(p.z, 1)) for p in placements}
    assert distinct_xz == {(-20.0, 0.0), (0.0, 0.0), (20.0, 0.0)}


def test_2x4_cannot_fully_nest_on_2x2():
    """A 2x4 has 8 receptors; a 2x2 has only 4 studs. No way to FULLY mate."""
    brick_2x4 = server.PART_INDEX["3001"]
    brick_2x2 = server.PART_INDEX["3003"]
    placements = find_placements_b_on_a(brick_2x2, brick_2x4, min_studs_matched=8)
    assert placements == []


def test_2x4_partial_overhang_on_2x2_works():
    """But the 2x4 CAN partially overhang the 2x2 (running bond)."""
    brick_2x4 = server.PART_INDEX["3001"]
    brick_2x2 = server.PART_INDEX["3003"]
    placements = find_placements_b_on_a(brick_2x2, brick_2x4, min_studs_matched=1)
    assert placements  # at least one partial-mate exists


def test_2x4_on_2x4_aligned_placements():
    """A 2x4 brick on top of an identical 2x4 brick has multiple ways: aligned
    full stacking + half-brick offsets (for running-bond)."""
    brick = server.PART_INDEX["3001"]
    placements = find_placements_b_on_a(brick, brick)
    distinct_xz = {(round(p.x, 1), round(p.z, 1)) for p in placements}
    # The full-overlap centered placement must be possible.
    assert (0.0, 0.0) in distinct_xz


def test_1x1_plate_on_1x1_plate():
    """1x1 plate on 1x1 plate: only one valid placement, dead-center."""
    plate = server.PART_INDEX["3024"]
    placements = find_placements_b_on_a(plate, plate)
    distinct_xz = {(round(p.x, 1), round(p.z, 1)) for p in placements}
    assert distinct_xz == {(0.0, 0.0)}


def test_2x2_tile_fully_on_2x2_plate():
    """A 2x2 tile (smooth top, no studs) FULLY nests on a 2x2 plate at center."""
    plate_2x2 = server.PART_INDEX["3022"]
    tile_2x2 = server.PART_INDEX["3068b"]
    placements = find_placements_b_on_a(plate_2x2, tile_2x2, min_studs_matched=4)
    distinct_xz = {(round(p.x, 1), round(p.z, 1)) for p in placements}
    assert distinct_xz == {(0.0, 0.0)}


def test_nothing_sits_on_a_tile():
    """A tile has no top studs — nothing can connect on top of it."""
    tile = server.PART_INDEX["3068b"]
    plate = server.PART_INDEX["3022"]
    assert find_placements_b_on_a(tile, plate) == []


def test_baseplate_supports_brick():
    """A 2x4 brick can sit on a 32x32 baseplate in many positions."""
    baseplate = server.PART_INDEX["3811"]
    brick = server.PART_INDEX["3001"]
    placements = find_placements_b_on_a(baseplate, brick)
    assert len(placements) > 50  # 32x32 baseplate has lots of valid positions


def test_find_connections_summary_includes_both_directions():
    a = server.PART_INDEX["3001"]   # 2x4 brick
    b = server.PART_INDEX["3003"]   # 2x2 brick
    # With full_nesting_only: 2x2 fully on 2x4 works; 2x4 doesn't fully fit on 2x2.
    r = find_connections(a, b, full_nesting_only=True)
    assert r["part_a"] == "3001"
    assert r["part_b"] == "3003"
    assert r["b_on_a_placements"]
    assert r["a_on_b_placements"] == []
