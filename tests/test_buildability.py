"""Buildability: strict add_part + floating-part detection."""

from __future__ import annotations

import pytest

from lego_mcp import server


def test_floating_part_detected():
    server.create_model()
    server.add_part("3001", "red", 0, -200, 0)  # high in the air, no support
    v = server.validate_model()
    assert v["summary"]["floating"] == 1


def test_grounded_brick_is_supported():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    v = server.validate_model()
    assert v["summary"]["floating"] == 0


def test_stacked_brick_is_supported_by_lower():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, -24, 0)
    v = server.validate_model()
    assert v["summary"]["floating"] == 0


def test_strict_add_rejects_collision():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    with pytest.raises(ValueError, match="strict.*collide"):
        server.add_part("3001", "blue", 0, 0, 0, strict=True)


def test_strict_add_rejects_floating():
    server.create_model()
    with pytest.raises(ValueError, match="strict.*support"):
        server.add_part("3001", "red", 0, -200, 0, strict=True)


def test_strict_add_accepts_grounded_brick():
    server.create_model()
    r = server.add_part("3001", "red", 0, 0, 0, strict=True)
    assert r["ok"]


def test_strict_add_accepts_proper_stack():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0, strict=True)
    r = server.add_part("3001", "blue", 0, -24, 0, strict=True)
    assert r["ok"]


def test_strict_rejects_too_small_overlap():
    """A brick with only a single corner over another isn't actually clutched —
    less than one stud's worth of contact. Strict mode should refuse it."""
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0, strict=True)
    # Try to put another brick on top with only a tiny overlap region
    # Lower brick AABB: x in [-40,40], z in [-20,20]
    # Upper brick at x=70 with depth 80 along Z extends z in [-20, 20].
    # AABB upper: x in [30, 110]. XZ overlap with lower: x [30,40], z [-20,20]
    # = 10 * 40 = 400 LDU². Exactly one stud — should be allowed barely.
    # Move it further so overlap < 400.
    with pytest.raises(ValueError, match="strict.*support"):
        server.add_part("3001", "blue", 75, -24, 0, strict=True)
