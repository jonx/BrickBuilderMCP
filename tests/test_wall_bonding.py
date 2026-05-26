"""Tests for the wall-bonding behavior of build_wall_segment and build_room."""

from __future__ import annotations

from lego_mcp import server
from lego_mcp import helpers
from lego_mcp.connection_graph import build_graph, vertical_seam_score, wall_bond_quality


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


def test_room_corner_courses_alternate_ownership():
    """Rows alternate which wall direction owns each corner."""
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    r = helpers.build_room(-80, -60, 80, 60, height_rows=4,
                            color="red", base_y=-4)
    assert r["ok"]
    assert r["wall_thickness_studs"] == 2
    row0 = r["rows"][0]["segments"]
    row1 = r["rows"][1]["segments"]
    assert [s["axis"] for s in row0] == ["x", "z", "x", "z"]
    assert [s["axis"] for s in row1] == ["x", "z", "x", "z"]
    assert [s["owns_corner"] for s in row0] == [True, False, True, False]
    assert [s["owns_corner"] for s in row1] == [False, True, False, True]
    # The owning edge spans the full outside dimension; the other direction is inset.
    assert row0[0]["length"] == 160
    assert row1[0]["length"] == 80
    assert row0[1]["length"] == 40
    assert row1[1]["length"] == 120


def test_room_corner_is_bonded_between_rows():
    """The row above bridges over the previous row's corner seam."""
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    helpers.build_room(-80, -60, 80, 60, height_rows=4,
                        color="red", base_y=-4)
    graph, _edges = build_graph(server.STATE.parts)
    row0_corner = next(
        p for p in server.STATE.parts.values()
        if p.y == -4 and p.rotation == "identity" and abs(p.x + 40) < 0.5 and abs(p.z + 40) < 0.5
    )
    row1_corner = next(
        p for p in server.STATE.parts.values()
        if p.y == -28 and p.rotation == "rot90y" and abs(p.x + 60) < 0.5 and abs(p.z + 20) < 0.5
    )
    assert row1_corner.instance_id in graph[row0_corner.instance_id]


def test_room_validates_as_connected_structure():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    helpers.build_room(-80, -60, 80, 60, height_rows=4,
                        color="red", base_y=-4)
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0
    assert v["summary"]["floating"] == 0
    assert v["summary"]["unanchored"] == 0


def test_room_can_be_built_with_only_2x4_bricks_when_dimensions_fit():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    r = helpers.build_room(-160, -120, 160, 120, height_rows=4,
                            color="red", base_y=-4, palette=["3001"])
    assert r["ok"]
    part_ids = {p.part_id for p in server.STATE.parts.values()}
    assert part_ids == {"3811", "3001"}
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0
    assert v["summary"]["floating"] == 0
    assert v["summary"]["unanchored"] == 0
    assert v["summary"]["vertical_seam_score"] == 0
    assert v["summary"]["wall_bond_quality"] == 1.0


def test_room_2x4_only_rejects_dimensions_that_need_fillers():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    try:
        helpers.build_room(-80, -60, 80, 60, height_rows=2,
                            color="red", base_y=-4, palette=["3001"])
    except ValueError as exc:
        assert "no brick fit" in str(exc)
    else:
        raise AssertionError("expected 2x4-only room to reject non-4-stud segment lengths")


def test_build_perimeter_supports_l_shaped_outline():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    r = helpers.build_perimeter(
        [[-240, -160], [240, -160], [240, 0], [80, 0], [80, 160], [-240, 160]],
        height_rows=4,
        color="red",
        base_y=-4,
    )
    assert r["ok"]
    assert len(r["points"]) == 6
    part_ids = {p.part_id for p in server.STATE.parts.values()}
    assert part_ids <= {"3811", "3001", "3002", "3003"}
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0
    assert v["summary"]["floating"] == 0
    assert v["summary"]["unanchored"] == 0


def test_build_perimeter_bonds_concave_corners_between_rows():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    helpers.build_perimeter(
        [[-240, -160], [240, -160], [240, 0], [80, 0], [80, 160], [-240, 160]],
        height_rows=2,
        color="red",
        base_y=-4,
    )
    graph, _edges = build_graph(server.STATE.parts)
    row0_bridge = next(
        p for p in server.STATE.parts.values()
        if p.y == -4 and p.rotation == "identity"
        and abs(p.x - 80) < 0.5 and abs(p.z + 20) < 0.5
    )
    row1_bridge = next(
        p for p in server.STATE.parts.values()
        if p.y == -28 and p.rotation == "rot90y"
        and abs(p.x - 60) < 0.5 and abs(p.z) < 0.5
    )
    assert row1_bridge.instance_id in graph[row0_bridge.instance_id]


def test_build_perimeter_rejects_diagonal_edges():
    server.create_model()
    try:
        helpers.build_perimeter([[0, 0], [80, 40], [80, 120], [0, 120]])
    except ValueError as exc:
        assert "diagonal" in str(exc)
    else:
        raise AssertionError("expected diagonal perimeter edge to be rejected")


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
