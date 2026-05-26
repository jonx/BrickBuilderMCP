"""Renderer smoke. Renders go to a temp dir, not the project renders/ folder."""

from __future__ import annotations

import os

from lego_mcp import server


def test_renderer_produces_png(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, -24, 0)
    server.add_part("3024", "yellow", 40, 0, 0)
    r = server.render_model(400, 300)
    assert r["ok"]
    from pathlib import Path
    p = Path(r["path"])
    assert p.exists()
    assert p.stat().st_size > 1000
    assert p.read_bytes().startswith(b"\x89PNG")
    # Sanity: written under the temp cwd, not project root.
    assert str(p).startswith(str(tmp_path))


def test_renderer_empty_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    server.create_model()
    r = server.render_model(200, 150)
    assert r["ok"]


def test_renderer_debug_color_modes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "red", 40, -24, 0)
    r = server.render_model(400, 300, color_mode="row", hidden_edges=True)
    assert r["ok"]
    assert r["color_mode"] == "row"
    assert r["hidden_edges"] is True
    from pathlib import Path
    assert Path(r["latest"]).exists()
