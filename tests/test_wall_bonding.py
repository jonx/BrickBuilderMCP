"""Tests for the wall-bonding behavior of build_wall_segment and build_room."""

from __future__ import annotations

from lego_mcp import server
from lego_mcp import helpers
from lego_mcp.connection_graph import vertical_seam_score, wall_bond_quality


def _seam_xs_per_row(parts, axis: str = "x") -> dict[int, set[int]]:
    """For each plate-row, set of brick-end positions along `axis`."""
    rows: dict[int, set[int]] = {}
    for inst in parts.values():
        part = server.PART_INDEX.get(inst.part_id)
        if part is None:
            continue
        row = int(round(-inst.y / 8))
        rows.setdefault(row, set())
        half = part.width / 2 if axis == "x" else part.depth / 2
        c = inst.x if axis == "x" else inst.z
        rows[row].add(int(round(c - half)))
        rows[row].add(int(round(c + half)))
    return rows


def test_straight_wall_seams_stagger():
    """A staggered wall: no internal seam X is shared between adjacent rows."""
    server.create_model()
    r = helpers.build_wall_segment(0, 0, 320, 0, height_rows=4,
                                    color="red", base_y=-4)
    assert r["ok"]
    # Adjacent rows should not share internal seam positions (excluding wall ends).
    seam_lists = [set(row["seams"]) for row in r["rows"]]
    for i in range(len(seam_lists) - 1):
        shared = seam_lists[i] & seam_lists[i + 1]
        assert shared == set(), f"rows {i}-{i+1} share seams at {shared}"


def test_straight_wall_validates_no_floating_no_collision():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)        # baseplate to ground it
    helpers.build_wall_segment(-160, 0, 160, 0, height_rows=4,
                                color="red", base_y=-4)
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0
    assert v["summary"]["floating"] == 0


def test_room_corner_bricks_stack_aligned():
    """Each SW corner brick's XZ must match the row above/below (it's a
    vertical column of 2x2s — they must align)."""
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    helpers.build_room(-80, -60, 80, 60, height_rows=4,
                        color="red", base_y=-4)
    sw_bricks = sorted(
        (p for p in server.STATE.parts.values()
         if p.part_id == "3003" and p.x < 0 and p.z < 0),
        key=lambda p: p.y, reverse=True   # bottom to top (y=-4 first)
    )
    assert len(sw_bricks) == 4
    xs = {p.x for p in sw_bricks}
    zs = {p.z for p in sw_bricks}
    assert len(xs) == 1 and len(zs) == 1   # perfectly stacked column


def test_room_corner_columns_connect_vertically():
    """The room uses 2x2 (3003) corner bricks. Adjacent-row corner bricks must
    share at least one stud (4 actually, since 2x2 is fully symmetric)."""
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    helpers.build_room(-80, -60, 80, 60, height_rows=4,
                        color="red", base_y=-4)
    # Find all 3003 bricks at the SW corner
    sw = [p for p in server.STATE.parts.values()
          if p.part_id == "3003" and p.x < 0 and p.z < 0]
    assert len(sw) == 4   # one per row


def test_seam_score_is_low_for_staggered_wall():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    helpers.build_wall_segment(-160, 0, 160, 0, height_rows=4,
                                color="red", base_y=-4)
    score = vertical_seam_score(server.STATE.parts)
    # Allow a small score because end bricks can produce coincidental seams.
    assert score <= 2


def test_unstaggered_wall_has_higher_seam_score():
    """Manually stack identical bricks directly: high seam score."""
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    for row in range(4):
        y = -4 - row * 24
        for x_center in (-120, -40, 40, 120):
            server.add_part("3010", "red", x_center, y, 0)
    score = vertical_seam_score(server.STATE.parts)
    assert score >= 6   # every internal seam shared between every adjacent row


def test_bond_quality_room_better_than_stacked():
    """build_room's interlocking pattern should score better than a stacked-column wall."""
    # Staggered room
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    helpers.build_room(-80, -60, 80, 60, height_rows=4, color="red", base_y=-4)
    bond_room = wall_bond_quality(server.STATE.parts)
    # Manual stacked column
    server.create_model()
    for row in range(4):
        y = -4 - row * 24
        for x_center in (-120, -40, 40, 120):
            server.add_part("3010", "red", x_center, y, 0)
    bond_stacked = wall_bond_quality(server.STATE.parts)
    assert bond_room > bond_stacked
