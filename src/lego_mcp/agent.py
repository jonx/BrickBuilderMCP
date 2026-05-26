"""Standalone agent: drive Claude with the LegoMCP server over stdio.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    uv run python -m lego_mcp.agent "build a small red house on a tan baseplate"

The agent spawns `lego-mcp` as a subprocess, fetches its tool list over MCP
JSON-RPC, hands those tools to Claude, and runs a tool-use loop until Claude
stops calling tools or the iteration cap is reached. Renders are written to
./renders/ as usual; the final render path is printed at the end.

This is a thin loop, not a planner/builder/critic split — Claude is doing
all three roles itself by following the system prompt. For more elaborate
orchestration, fork this file and split per phase.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any


SYSTEM_PROMPT = """\
You are driving the LegoMCP toolchain to build a real LEGO model that opens
in BrickLink Studio / LeoCAD.

Rules (non-negotiable, this is how real LEGO works):
1. Every brick must be physically connected. Either (a) it sits on the
   ground plane (y=0), or (b) it has at least one stud's worth (>= 400 LDU²
   of XZ overlap) with another part directly above or below it.
2. Bricks may not overlap each other.
3. LDraw convention: -Y is up. 1 stud = 20 LDU wide. 1 brick = 24 LDU tall.
   "Brick 2x4" is 80 LDU along +X (long axis), 40 along +Z.
4. Rotations are named: identity, rot90y, rot180y, rot270y, rot90x, rot90z.

Workflow:
- Start with create_model(name).
- Use search_parts(query) to find pieces by description.
- Place a baseplate (3811 = 32x32, top face at y=-4) before building.
- For walls: use build_wall(x0,z0,x1,z1, height_rows, color, bond,
   inset_ends, base_y=-4). For rectangles of walls: build_room.
- For floors: build_floor.
- After ~20 placements: validate_model(). If collisions/floating/unanchored
   appear, fix BEFORE continuing. render_model() to see what you built.
- Use save_checkpoint("desc") before risky changes; restore_checkpoint to
   roll back.

Stop calling tools when the build matches the user's goal. Don't keep
adding parts past the goal.
"""


def _send(proc: subprocess.Popen, msg: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(msg) + "\n").encode())
    proc.stdin.flush()


def _read_until(proc: subprocess.Popen, want_id: int, timeout: float = 60.0) -> dict:
    import time
    assert proc.stdout is not None
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("id") == want_id:
            return m
    raise RuntimeError(f"MCP server did not reply id={want_id} within {timeout}s")


def _mcp_handshake(proc: subprocess.Popen) -> list[dict]:
    """Initialize the MCP session and return its tool list."""
    _send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lego-mcp-agent", "version": "1"},
        },
    })
    _read_until(proc, 1)
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    resp = _read_until(proc, 2)
    return resp["result"]["tools"]


def _call_mcp_tool(proc: subprocess.Popen, call_id: int,
                    name: str, args: dict) -> dict:
    _send(proc, {
        "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    resp = _read_until(proc, call_id)
    if "error" in resp:
        return {"error": resp["error"]}
    return resp.get("result", {})


def _mcp_to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Convert MCP tool list to Anthropic Messages tool spec."""
    out = []
    for t in tools:
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        })
    return out


def run_session(goal: str, *, model: str = "claude-sonnet-4-6",
                 max_iterations: int = 60) -> dict[str, Any]:
    """Spawn the MCP server, drive Claude, return a summary."""
    try:
        import anthropic
    except ImportError:
        sys.exit("Install the agent extra: `uv sync --extra agent`")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY in your environment.")

    proc = subprocess.Popen(
        [sys.executable, "-m", "lego_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        tools_mcp = _mcp_handshake(proc)
        tools_anthropic = _mcp_to_anthropic_tools(tools_mcp)
        print(f"[agent] MCP server up; {len(tools_anthropic)} tools available.")

        client = anthropic.Anthropic(api_key=api_key)
        messages: list[dict] = [{"role": "user", "content": goal}]
        call_id = 100

        for iteration in range(max_iterations):
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=tools_anthropic,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            text_blocks = [b for b in resp.content if b.type == "text"]

            for tb in text_blocks:
                print(f"[claude] {tb.text}")

            if not tool_uses:
                print(f"[agent] Claude stopped calling tools after {iteration + 1} iteration(s).")
                break

            tool_results = []
            for tu in tool_uses:
                print(f"[tool] {tu.name}({json.dumps(tu.input)})")
                call_id += 1
                result = _call_mcp_tool(proc, call_id, tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result),
                })
            messages.append({"role": "user", "content": tool_results})
        else:
            print(f"[agent] Reached max_iterations ({max_iterations}); stopping.")

        # Save the model so the user can inspect.
        call_id += 1
        save = _call_mcp_tool(proc, call_id, "export_mpd",
                               {"path": "./agent_build.mpd"})
        call_id += 1
        render = _call_mcp_tool(proc, call_id, "render_model",
                                 {"width": 1200, "height": 900})
        return {"ok": True, "iterations": iteration + 1,
                "saved_mpd": save, "render": render,
                "tool_calls": call_id - 100}
    finally:
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("goal", help='What to build, e.g. "a small red house"')
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--max-iterations", type=int, default=60)
    args = parser.parse_args()

    summary = run_session(args.goal, model=args.model,
                           max_iterations=args.max_iterations)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
