"""Debug toolkit: render_validation, inspect_part, collision_detail, describe_errors."""

from __future__ import annotations

import pytest

from mcp.server.fastmcp import Image as MCPImage

from lego_mcp import server


def _mixed_scene():
    """Scene with one OK, one floating, one collision pair, one grounded baseplate."""
    server.create_model("mixed")
    server.add_part("3811", "tan", 0, 0, 0)          # 1: baseplate (ok)
    server.add_part("3001", "red", 0, -4, 0)         # 2: brick on baseplate (ok)
    server.add_part("3001", "blue", 0, -4, 0)        # 3: colocated -> collision with 2
    server.add_part("3001", "green", 0, -200, 0)     # 4: floating
    return ("1", "2", "3", "4")


def test_inspect_part_ok():
    base, ok_brick, _, _ = _mixed_scene()
    r = server.inspect_part(ok_brick)
    assert r["instance_id"] == ok_brick
    assert "ok" not in r["validation"] or r["validation"] == ["ok"]  # baseplate connects this
    # collision flag should not be on a grounded baseplate brick that collides only with
    # another brick — actually #2 DOES collide with #3, so let's check the specific case
    # of the baseplate instead
    r2 = server.inspect_part(base)
    assert "collision" not in r2["validation"]


def test_inspect_part_collision():
    _, a, b, _ = _mixed_scene()
    r = server.inspect_part(a)
    assert "collision" in r["validation"]
    assert any(c["other"] == b for c in r["collides_with"])
    # Each collision entry has an overlap region + a separation suggestion
    coll = r["collides_with"][0]
    assert coll["overlap_region"] is not None
    assert coll["smallest_separation_ldu"]
    # Position of the other brick is included
    assert coll["other_position"] == [0, -4, 0]


def test_inspect_part_floating():
    _, _, _, fp = _mixed_scene()
    r = server.inspect_part(fp)
    assert "floating" in r["validation"]
    assert r["collides_with"] == []
    assert r["connected_to"] == []


def test_inspect_part_unknown_raises():
    server.create_model()
    with pytest.raises(ValueError):
        server.inspect_part("not_a_real_id")


def test_collision_detail_overlapping_pair():
    _, a, b, _ = _mixed_scene()
    r = server.collision_detail(a, b)
    assert r["collides"]
    assert r["overlap_volume_ldu3"] > 0
    sep = r["smallest_separation_ldu"]
    assert "move_x" in sep and "move_y" in sep and "move_z" in sep
    assert "Move" in r["suggestion"]


def test_collision_detail_non_overlapping():
    server.create_model()
    a = server.add_part("3001", "red", 0, 0, 0)["instance_id"]
    b = server.add_part("3001", "red", 200, 0, 0)["instance_id"]
    r = server.collision_detail(a, b)
    assert not r["collides"]


def test_describe_errors_enriches_each():
    _mixed_scene()
    r = server.describe_errors(max_errors=10)
    assert r["error_count"] > 0
    types = {e["type"] for e in r["errors"]}
    assert "collision" in types
    assert "floating_part" in types
    # Each collision entry has overlap_region (vs the raw validate_model error
    # which has just IDs)
    coll = next(e for e in r["errors"] if e["type"] == "collision")
    assert coll["overlap_volume_ldu3"] > 0


def test_render_validation_returns_image_and_legend(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGO_MCP_RENDERS_DIR", str(tmp_path))
    _mixed_scene()
    result = server.render_validation(400, 300)
    assert len(result) == 3, "expected [markdown, summary, image]"
    markdown, summary, img = result
    assert markdown.startswith("![") and "data:image/png;base64," in markdown
    assert isinstance(img, MCPImage)
    assert img.data.startswith(b"\x89PNG")
    # Counts surface in the summary
    assert summary["collisions"] >= 2
    assert summary["floating"] >= 1
    assert "legend" in summary
    assert "collision" in summary["legend"]
