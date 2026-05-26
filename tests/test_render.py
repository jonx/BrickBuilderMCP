"""Renderer smoke. Renders return [summary_dict, MCPImage] so the LLM
sees the image inline and the human gets the disk path."""

from __future__ import annotations

from mcp.server.fastmcp import Image as MCPImage

from lego_mcp import server


def _summary_and_image(result):
    """Pull the markdown text, dict, and MCPImage out of a render tool's
    [markdown, summary, image] return."""
    assert len(result) == 3, f"expected 3 content blocks, got {len(result)}"
    markdown, summary, image = result
    assert isinstance(markdown, str)
    assert markdown.startswith("!["), "first block should be markdown image"
    assert "data:image/png;base64," in markdown, "should be inline data URI"
    assert isinstance(summary, dict)
    assert isinstance(image, MCPImage)
    return summary, image


def test_render_returns_summary_dict_and_image(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGO_MCP_RENDERS_DIR", str(tmp_path))
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "blue", 0, -24, 0)
    server.add_part("3024", "yellow", 40, 0, 0)
    summary, image = _summary_and_image(server.render_model(400, 300))
    assert summary["ok"]
    assert image.data.startswith(b"\x89PNG")
    assert summary["bytes"] == len(image.data)
    from pathlib import Path
    p = Path(summary["path"])
    assert p.exists()
    assert p.stat().st_size > 1000
    assert str(p).startswith(str(tmp_path))


def test_render_empty_model_still_returns_image(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGO_MCP_RENDERS_DIR", str(tmp_path))
    server.create_model()
    summary, image = _summary_and_image(server.render_model(200, 150))
    assert summary["ok"]
    assert image.data.startswith(b"\x89PNG")


def test_render_debug_color_modes(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGO_MCP_RENDERS_DIR", str(tmp_path))
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.add_part("3001", "red", 40, -24, 0)
    summary, _ = _summary_and_image(
        server.render_model(400, 300, color_mode="row", hidden_edges=True)
    )
    assert summary["color_mode"] == "row"
    assert summary["hidden_edges"] is True
    from pathlib import Path
    assert Path(summary["latest"]).exists()


def test_view_latest_render_returns_existing_png(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGO_MCP_RENDERS_DIR", str(tmp_path))
    server.create_model()
    server.add_part("3001", "red", 0, 0, 0)
    server.render_model(200, 150)
    result = server.view_latest_render()
    summary, image = _summary_and_image(result)
    assert summary["ok"]
    assert image.data.startswith(b"\x89PNG")


def test_view_latest_render_without_prior_render(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGO_MCP_RENDERS_DIR", str(tmp_path))
    result = server.view_latest_render()
    assert isinstance(result, list)
    assert result[0]["ok"] is False
