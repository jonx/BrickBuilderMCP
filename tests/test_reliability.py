"""Connection-reliability regressions.

These pin down the failure modes that used to let physically impossible
models pass validation:
- the validator only knew connectors for 10 hardcoded part types;
- an AABB "sits on a grounded part" fallback blessed unconnected parts;
- add_part gave no connectivity feedback, so errors compounded silently;
- wall helpers mixed 1-wide and 2-wide bricks that never clutch.
"""

from __future__ import annotations

import pytest

from lego_mcp import helpers, server


# ---------------------------------------------------------------------------
# Validator ground truth: stud mating, not AABB contact
# ---------------------------------------------------------------------------

def test_half_stud_offset_is_invalid():
    """A brick resting half a stud off on top of another has zero clutch.
    The old AABB anchor fallback used to mark this model valid."""
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 10, -24, 0)
    r = server.validate_model()
    assert not r["valid"]
    assert r["summary"]["floating"] == 1
    assert r["summary"]["connections"] == 0


def test_one_wide_centered_on_two_wide_is_invalid():
    """1x4 centered on a 2x4: receiver row at z=0 sits between the stud
    columns at z=±10. Rests, but never clutches."""
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3010", "blue", 0, -24, 0)
    r = server.validate_model()
    assert not r["valid"]
    assert r["summary"]["floating"] == 1


def test_parts_outside_legacy_subset_connect():
    """1x6 (3009) was invisible to the old 10-part connector table. A 1x6
    bridging two 1x4s on a shared stud row must validate as connected."""
    server.create_model()
    server.add_part("3010", "red", 0, 0, 0)
    server.add_part("3010", "red", 80, 0, 0)
    server.add_part("3009", "blue", 40, -24, 0)
    r = server.validate_model()
    assert r["valid"]
    assert r["summary"]["connections"] == 2
    assert r["summary"]["floating"] == 0


def test_brick_on_baseplate_connects():
    server.create_model()
    server.add_part("3811", "tan", 0, 0, 0)
    server.add_part("3001", "red", 0, -4, 0)
    r = server.validate_model()
    assert r["valid"]
    assert r["summary"]["connections"] == 1


# ---------------------------------------------------------------------------
# Building down: hanging under a part is a real connection
# ---------------------------------------------------------------------------

def _build_overhang():
    """Ground column two bricks high plus a 2x4 cantilevered at the top.
    Returns the overhang's instance id."""
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "green", 0, -24, 0)
    return server.add_part("3001", "yellow", 40, -48, 0)["instance_id"]


def test_hanging_brick_validates():
    _build_overhang()
    server.add_part("3004", "blue", 60, -24, 10)   # 1x2 hung under the overhang
    r = server.validate_model()
    assert r["valid"]
    assert r["summary"]["floating"] == 0
    assert r["summary"]["unanchored"] == 0


def test_strict_accepts_hanging_brick():
    _build_overhang()
    r = server.add_part("3004", "blue", 60, -24, 10, strict=True)
    assert r["ok"]
    assert r["connectivity"]["is_connected"]


# ---------------------------------------------------------------------------
# Per-mutation feedback
# ---------------------------------------------------------------------------

def test_add_part_reports_connectivity():
    server.create_model()
    a = server.add_part("3001", "red", 0, 0, 0)
    assert a["connectivity"]["grounded"]
    assert a["warnings"] == []
    b = server.add_part("3001", "blue", 0, -24, 0)
    assert b["connectivity"]["connected_to"] == {a["instance_id"]: 8}
    assert b["connectivity"]["studs_engaged"] == 8
    assert b["warnings"] == []


def test_add_part_warns_on_floating():
    server.create_model()
    r = server.add_part("3001", "red", 0, -200, 0)   # non-strict still places it
    assert not r["connectivity"]["is_connected"]
    assert any("FLOATING" in w for w in r["warnings"])


def test_add_part_warns_on_collision():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    r = server.add_part("3001", "blue", 20, 0, 0)    # overlaps, non-strict
    assert r["collisions"]
    assert any("COLLISION" in w for w in r["warnings"])


def test_move_part_reports_connectivity():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    b = server.add_part("3001", "blue", 0, -24, 0)["instance_id"]
    r = server.move_part(b, 0, -200, 0)
    assert any("FLOATING" in w for w in r["warnings"])
    r = server.move_part(b, 0, -24, 0)
    assert r["warnings"] == []
    assert r["connectivity"]["studs_engaged"] == 8


def test_rotate_part_reports_connectivity():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    b = server.add_part("3001", "blue", 0, -24, 0)["instance_id"]
    r = server.rotate_part(b, "rot90y")   # 2x4 crossed on a 2x4: 4 studs mate
    assert r["connectivity"]["studs_engaged"] == 4


# ---------------------------------------------------------------------------
# Placement by reference
# ---------------------------------------------------------------------------

def test_find_valid_placements_tokens_and_strict_add():
    server.create_model()
    base = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    r = helpers.find_valid_placements("3003", base, limit=10)
    assert r["count"] > 0
    best = r["placements"][0]
    assert best["studs_matched"] == 4          # 2x2 fully nested on a 2x4
    placed = helpers.add_part_at_placement(best["token"], "blue")
    assert placed["connectivity"]["is_connected"]
    assert server.validate_model()["valid"]


def test_find_valid_placements_filters_collisions_and_offers_underneath():
    overhang = _build_overhang()
    r = helpers.find_valid_placements("3003", overhang, limit=40)
    # Placements colliding with the column below the overhang are dropped.
    assert r["filtered_out_colliding"] > 0
    under = [p for p in r["placements"] if p["direction"] == "underneath"]
    assert under, "expected hang-under placements for a cantilevered anchor"
    placed = helpers.add_part_at_placement(under[0]["token"], "blue")
    assert placed["connectivity"]["is_connected"]
    assert server.validate_model()["valid"]


def test_find_valid_placements_dedupes_square_rotations():
    server.create_model()
    base = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    r = helpers.find_valid_placements("3003", base, limit=40)
    positions = [(p["x"], p["y"], p["z"]) for p in r["placements"]]
    assert len(positions) == len(set(positions))


def test_stale_placement_token_fails_loudly():
    server.create_model()
    with pytest.raises(ValueError, match="placement token"):
        helpers.add_part_at_placement("pl999999", "red")


def test_find_valid_placements_respects_rotated_anchor():
    server.create_model()
    anchor = server.add_part("3001", "red", 0, 0, 0, rotation="rot90y")["instance_id"]
    r = helpers.find_valid_placements("3004", anchor, limit=5)
    assert r["count"] > 0
    placed = helpers.add_part_at_placement(r["placements"][0]["token"], "white")
    assert placed["connectivity"]["is_connected"]


# ---------------------------------------------------------------------------
# Wall helpers must produce clutched walls
# ---------------------------------------------------------------------------

def test_mixed_width_palette_rejected():
    server.create_model()
    with pytest.raises(ValueError, match="uniform-width"):
        helpers.build_wall_segment(0, 0, 320, 0, height_rows=2,
                                   palette=["3001", "3004"])


def test_build_wall_produces_fully_connected_wall():
    server.create_model()
    helpers.build_wall(0, 0, 320, 0, height_rows=3, color="red", bond="running")
    r = server.validate_model()
    assert r["valid"]
    assert r["summary"]["floating"] == 0
    assert r["summary"]["unanchored"] == 0
    assert r["summary"]["connections"] > 0
