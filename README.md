# LegoMCP

An MCP server that lets an LLM design real LEGO models — by emitting validated LDraw files, not by clicking around in a CAD app.

Drop it into Claude Desktop (or any MCP client), say "build me a small castle", and the LLM gets a set of semantic tools: `add_part`, `move_part`, `validate_model`, `export_mpd`, undo/redo, checkpoints. The model state lives in the server. The output is a real `.ldr` / `.mpd` file you can open in [BrickLink Studio](https://www.bricklink.com/v3/studio/download.page), [LeoCAD](https://www.leocad.org/), or any LDraw viewer.

## Why this exists

The naive approach to LLM-built LEGO is to let the model drive a CAD app via screen automation. That fails — too many micro-actions, no spatial ground truth, errors compound. LegoMCP gives the LLM a *semantic* API: every action is a typed function call, every change is validated, every state is reversible.

The CAD app becomes the viewer. The MCP server is the source of truth.

## Quick start

```bash
# 1. Install uv if you don't have it
brew install uv     # macOS
# or: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install LegoMCP as a uv tool
uv tool install --from . lego-mcp

# 3. Download the LDraw parts library (~85 MB, one-time)
lego-mcp install-library

# 4. Point Claude Desktop at it
#    Add to ~/Library/Application Support/Claude/claude_desktop_config.json:
{
  "mcpServers": {
    "lego": {
      "command": "lego-mcp"
    }
  }
}
```

Restart Claude Desktop. Ask Claude: *"List the parts in your current LEGO model."* If you get an empty list back, you're wired up.

## What the LLM can do

| Tool | What it does |
|---|---|
| `create_model(name)` | Start a fresh model |
| `add_part(part_id, color, x, y, z, rotation)` | Place a brick. Rotation is one of `identity`, `rot90y`, `rot180y`, `rot270y`, `rot90x`, `rot90z` |
| `remove_part(instance_id)` | Remove by stable instance ID |
| `move_part(instance_id, x, y, z)` | Reposition |
| `list_parts()` | Inspect current model |
| `search_parts(query)` | Find part IDs by name |
| `get_part_info(part_id)` | Dimensions, AABB, description |
| `validate_model()` | Collision + reference checks |
| `export_ldr(path)` / `export_mpd(path)` | Write to disk |
| `import_ldr(path)` | Load an existing model |
| `undo()` / `redo()` | |
| `save_checkpoint(name)` / `restore_checkpoint(name)` | Named in-memory snapshots |

## Coordinate system

LDraw convention:

- **1 brick** = 20 LDU wide × 24 LDU tall
- **-Y is up** (right-handed)
- **+X right, +Z forward**

So a 2×4 brick sitting on the studs of another 2×4 brick is at `y = -24` (negative because up is `-Y`).

## What this is not

- It does not check stud-clutch connectivity (yet — would need real LDraw connection-point parsing).
- It does not estimate stability (no physics).
- It does not render images (Phase 2 — will shell to LDView).
- It does not automate BrickLink Studio. Open the exported `.mpd` manually.

## License

MIT. See [LICENSE](LICENSE).
