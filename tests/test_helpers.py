"""High-level building helpers: build_wall, build_floor, repeat_pattern."""

from __future__ import annotations

from lego_mcp import helpers, server


def test_build_wall_running_bond_alternates():
    server.create_model()
    r = helpers.build_wall(0, 0, 320, 0, height_rows=2, color="red", bond="running")
    assert r["ok"]
    # New shape: rows is a list of {y, bricks, seams}. >=3 bricks per row.
    assert r["bricks_placed"] >= 6
    assert r["rows"][0]["bricks"] >= 3
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0


def test_build_wall_z_running_works_too():
    server.create_model()
    r = helpers.build_wall(0, 0, 0, 320, height_rows=2, color="blue", bond="stretcher")
    assert r["ok"]
    assert r["bricks_placed"] >= 6
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0


def test_build_floor_tiles_a_rectangle():
    server.create_model()
    r = helpers.build_floor(-80, -80, 80, 80, y=-4, color="tan", part_id="3022")
    assert r["ok"]
    # 160x160 / 40x40 (2x2 plate) = 4x4 = 16 plates
    assert r["plates"] == 16
    v = server.validate_model()
    assert v["summary"]["collisions"] == 0


def test_repeat_pattern_lines_up_parts():
    server.create_model()
    r = helpers.repeat_pattern("3005", count=5, dx=40, color="yellow")
    assert r["placed"] == 5
    # All five at the same Y/Z, X stepping by 40
    parts = sorted(server.STATE.parts.values(), key=lambda p: p.x)
    assert [p.x for p in parts] == [0, 40, 80, 120, 160]
