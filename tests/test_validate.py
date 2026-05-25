"""Validation: collisions, unknown parts, rotations."""

from __future__ import annotations

from lego_mcp import server


def test_clean_model_validates():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)         # 2x4 brick
    server.add_part("3001", "blue", 100, 0, 0)      # well away
    r = server.validate_model()
    assert r["valid"] is True
    assert r["summary"]["collisions"] == 0


def test_overlapping_parts_detected():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, 0, 0)        # full overlap
    r = server.validate_model()
    assert r["valid"] is False
    assert r["summary"]["collisions"] == 1


def test_stacking_does_not_collide():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)         # brick on ground (y=0 to y=-24)
    server.add_part("3001", "blue", 0, -24, 0)      # brick stacked on top (y=-24 to y=-48)
    r = server.validate_model()
    assert r["summary"]["collisions"] == 0


def test_rotation_swaps_footprint():
    server.create_model()
    # Two 2x4 bricks placed side by side along Z. Without rotation they don't touch.
    server.add_part("3001", "red", 0, 0, 0)         # width 40 (X), depth 80 (Z)
    server.add_part("3001", "blue", 0, 0, 100)      # 100 LDU away in Z → no overlap
    assert server.validate_model()["summary"]["collisions"] == 0

    # Rotate one 90deg around Y -> swaps X and Z. New footprint is 80 (X) x 40 (Z).
    # Centered on (40,0,0), it extends X in [0, 80] -> overlaps the red brick at X in [-20, 20].
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 40, 0, 0, rotation="rot90y")
    r = server.validate_model()
    assert r["summary"]["collisions"] == 1
