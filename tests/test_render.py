"""Renderer smoke."""

from __future__ import annotations

from lego_mcp import server


def test_renderer_produces_png():
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, -24, 0)
    server.add_part("3024", "yellow", 40, 0, 0)
    r = server.render_model(400, 300)
    assert r["ok"]
    from pathlib import Path
    p = Path(r["path"])
    assert p.exists()
    assert p.stat().st_size > 1000  # at least a real PNG, not empty
    assert p.read_bytes().startswith(b"\x89PNG")


def test_renderer_empty_model():
    server.create_model()
    r = server.render_model(200, 150)
    assert r["ok"]
