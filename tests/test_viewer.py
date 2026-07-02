"""MCP App viewer: geometry tool + ui:// resource + tool/UI link."""
import asyncio
from lego_mcp import server


def test_open_viewer_geometry():
    server.create_model("v", include_manual=False)
    server.add_part("3811", "green", 0, 0, 0)
    server.add_part("3001", "red", 10, -4, 10)
    g = server.open_viewer()
    assert g["count"] == 2
    brick = g["parts"][1]
    assert brick["dims"] == [80.0, 24.0, 40.0]
    assert brick["color"].startswith("rgb(")


def test_viewer_ui_wiring():
    tools = asyncio.run(server.mcp.list_tools())
    ov = next(t for t in tools if t.name == "open_viewer")
    assert ov.meta["ui"]["resourceUri"] == "ui://lego/viewer.html"
    res = asyncio.run(server.mcp.list_resources())
    assert any(str(r.uri) == "ui://lego/viewer.html" for r in res)
    html = server._viewer_html()
    assert "canvas" in html and "http" not in html   # self-contained bundle
