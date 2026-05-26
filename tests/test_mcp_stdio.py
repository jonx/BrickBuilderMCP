"""End-to-end smoke: launch the server as a real subprocess, speak MCP JSON-RPC.

This verifies the wire protocol works (not just direct function calls), which is
what Claude Desktop / Claude Code actually does.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time


def _send(proc: subprocess.Popen, msg: dict) -> None:
    line = json.dumps(msg) + "\n"
    assert proc.stdin is not None
    proc.stdin.write(line.encode())
    proc.stdin.flush()


def _read_until(proc: subprocess.Popen, want_id: int, timeout: float = 5.0) -> dict:
    """Read newline-delimited JSON until we see a response with the given id."""
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == want_id:
            return msg
    raise AssertionError(f"Did not receive response id={want_id} within {timeout}s")


def test_server_initializes_and_calls_tool():
    proc = subprocess.Popen(
        [sys.executable, "-m", "lego_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # 1. initialize
        _send(proc, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        })
        resp = _read_until(proc, 1)
        assert "result" in resp, f"init failed: {resp}"
        assert "serverInfo" in resp["result"]

        # 2. initialized notification
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. list tools — confirm our key tools are exposed
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = _read_until(proc, 2)
        names = {t["name"] for t in resp["result"]["tools"]}
        # Must-have tools
        for required in [
            "create_model", "add_part", "remove_part", "move_part",
            "list_parts", "validate_model",
            "export_ldr", "export_mpd", "import_ldr",
            "undo", "redo",
            "save_checkpoint", "restore_checkpoint",
            "render_model",  # only registers if Pillow is importable
        ]:
            assert required in names, f"tool {required!r} not exposed; got {names}"

        # 3b. list prompts — verify our build/from_plans/etc. templates are exposed
        _send(proc, {"jsonrpc": "2.0", "id": 25, "method": "prompts/list"})
        resp = _read_until(proc, 25)
        prompt_names = {p["name"] for p in resp["result"]["prompts"]}
        for expected in ("build", "from_plans", "from_image", "rescue", "techniques"):
            assert expected in prompt_names, f"prompt {expected!r} missing; got {prompt_names}"

        # 4. create_model + add_part — verify a real round-trip call works
        _send(proc, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "create_model", "arguments": {"name": "smoke"}},
        })
        assert "result" in _read_until(proc, 3)

        _send(proc, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "add_part", "arguments": {
                "part_id": "3001", "color": "red", "x": 0, "y": 0, "z": 0,
            }},
        })
        resp = _read_until(proc, 4)
        assert "result" in resp, f"add_part failed: {resp}"
        # FastMCP wraps tool output in content blocks; just confirm no error.
        assert not resp["result"].get("isError"), resp
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
