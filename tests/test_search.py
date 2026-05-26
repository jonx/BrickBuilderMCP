"""Search normalization: tolerate 'tile 1x4' / 'Tile 1 x 4' etc."""

from __future__ import annotations

from lego_mcp.parts import BUILTIN_PARTS, _normalize, search


def test_normalize_collapses_size_patterns():
    assert _normalize("Tile 1 x 4") == "tile 1x4"
    assert _normalize("Brick  2  x  4") == "brick 2x4"
    assert _normalize("Slope 33 3x6") == "slope 33 3x6"
    # 3D dimensions: 2 x 4 x 3
    assert _normalize("Window 2 x 4 x 3") == "window 2x4x3"


def test_search_tolerates_size_spacing():
    hits = search(BUILTIN_PARTS, "brick 2x4")
    ids = {p.part_id for p in hits}
    assert "3001" in ids


def test_search_token_order_does_not_matter():
    # Built-in name is "Brick 2x4"; query "2x4 brick" should match too.
    hits = search(BUILTIN_PARTS, "2x4 brick")
    ids = {p.part_id for p in hits}
    assert "3001" in ids


def test_search_empty_returns_nothing():
    assert search(BUILTIN_PARTS, "") == []
    assert search(BUILTIN_PARTS, "   ") == []


def test_search_matches_part_id():
    hits = search(BUILTIN_PARTS, "3001")
    assert any(p.part_id == "3001" for p in hits)
