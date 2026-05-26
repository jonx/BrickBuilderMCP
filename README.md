# LegoMCP

An [MCP](https://modelcontextprotocol.io) server that lets an LLM design real LEGO models — by emitting validated LDraw files, not by clicking around in a CAD app.

Drop it into Claude Desktop (or any MCP client), say *"build me a small castle"*, and the LLM gets a set of semantic tools: `add_part`, `move_part`, `validate_model`, `render_model`, `export_mpd`, undo/redo, checkpoints. The model state lives in the server. The output is a real `.ldr` / `.mpd` file you can open in [BrickLink Studio](https://www.bricklink.com/v3/studio/download.page), [LeoCAD](https://www.leocad.org/), or any LDraw viewer.

The built-in isometric renderer also writes a PNG to `./renders/<timestamp>_<model>.png` after every `render_model` call, so the LLM can see what it built and the history of the model is preserved.

> Status: alpha. Works end-to-end with Claude Desktop. The built-in catalog has 36 common parts; run `lego-mcp install-library` to add the full ~20k LDraw catalog.

## Why this exists

The naive approach to LLM-built LEGO is to let the model drive a CAD app via screen automation. That fails — too many micro-actions, no spatial ground truth, errors compound. LegoMCP gives the LLM a *semantic* API: every action is a typed function call, every change is validated, every state is reversible.

The CAD app becomes the viewer. The MCP server is the source of truth.

## Quick start

```bash
# 1. Install uv if you don't have it
brew install uv                # macOS
# or: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone and install LegoMCP
git clone <this repo> LegoMCP && cd LegoMCP
uv tool install --from . lego-mcp

# 3. (Optional) Download the full LDraw parts library (~85 MB).
#    Skip this for now — you get 36 built-in parts and can come back later.
lego-mcp install-library

# 4. Point Claude Desktop at the server.
#    Edit ~/Library/Application Support/Claude/claude_desktop_config.json:
{
  "mcpServers": {
    "lego": {
      "command": "lego-mcp"
    }
  }
}
```

Restart Claude Desktop. Ask Claude:

> *List the parts in your current LEGO model, then build me a small red house on a tan baseplate.*

Then ask:

> *Render the model.*

You'll find the PNG in `./renders/latest.png`.

## What the LLM can do

| Tool | What it does |
|---|---|
| `create_model(name)` | Start a fresh model |
| `add_part(part_id, color, x, y, z, rotation)` | Place a brick. Returns the new `instance_id`. |
| `remove_part(instance_id)` | Remove by ID |
| `move_part(instance_id, x, y, z)` | Reposition |
| `rotate_part(instance_id, rotation)` | Change orientation |
| `list_parts()` | Inspect the current model |
| `search_parts(query)` | Find part IDs by name ("brick 2x4") |
| `get_part_info(part_id)` | Dimensions, name |
| `list_colors()` | Supported color names and IDs |
| `validate_model()` | AABB collision + unknown-part check |
| `export_ldr(path)` / `export_mpd(path)` | Write to disk |
| `import_ldr(path)` | Load an existing `.ldr` or `.mpd` |
| `render_model(width, height)` | Built-in isometric PNG to `./renders/<timestamp>.png` |
| `undo()` / `redo()` | Snapshot-based, ~200 steps |
| `save_checkpoint(name)` / `restore_checkpoint(name)` / `list_checkpoints()` | Named in-memory snapshots |

### Rotations

Six canonical orientations (LEGO almost always wants 90° increments):

| Name | Effect |
|---|---|
| `identity` | No rotation |
| `rot90y`, `rot180y`, `rot270y` | Rotate around the Y axis (vertical) |
| `rot90x` | Rotate around X (tips a brick onto its side) |
| `rot90z` | Rotate around Z |

### Coordinate convention (LDraw)

- Right-handed, **-Y is up.**
- 1 stud = **20 LDU** wide.
- 1 plate = **8 LDU** tall.
- 1 brick = **24 LDU** tall (= 3 plates).
- Part origins are at the **center of the bottom face**.

So a 2×4 brick at `y=0` sits on the ground; stack another on top at `y=-24`.

## Example

```python
# (or have Claude do the same via tool calls)
import lego_mcp.server as s

s.create_model("tiny_house")
s.add_part("3811", "tan", 0, 0, 0)                                # 32x32 baseplate

# A 6x6-stud building. A 2x4 brick is 80 LDU along +X and 40 along +Z.
for y in [-12, -36, -60]:                                         # 3 brick rows
    for i in (-1, 0, 1):
        s.add_part("3001", "red", i*80, y,  100)                  # front wall
        s.add_part("3001", "red", i*80, y, -100)                  # back wall
    for j in (-1, 1):
        s.add_part("3001", "red",  100, y, j*40, rotation="rot90y")  # right
        s.add_part("3001", "red", -100, y, j*40, rotation="rot90y")  # left

# Gabled roof
for i in (-1, 0, 1):
    s.add_part("3037", "blue", i*80, -84,  100)
    s.add_part("3037", "blue", i*80, -84, -100, rotation="rot180y")

s.render_model()
```

See [examples/tiny_house.mpd](examples/tiny_house.mpd) for the exported model — open it in BrickLink Studio or LeoCAD.

## Development

```bash
uv sync --extra dev
uv run pytest         # full suite, includes a real MCP stdio handshake test
```

Architecture and decisions live in [NOTES.md](NOTES.md).

## What this is not (yet)

- **No stud-clutch validation.** AABB collision catches gross overlaps; doesn't verify that bricks actually snap together.
- **No stability estimation.** A floating tower will pass validation.
- **No subassembly authoring tools.** You can import multi-block MPDs (they get flattened); we author single-block files for now.
- **Built-in renderer is AABB-only.** For photoreal renders, open the exported `.mpd` in BrickLink Studio.

The [Strasbourg Cathedral astronomical clock](https://en.wikipedia.org/wiki/Strasbourg_astronomical_clock) is the north star — see NOTES.md for what gets added on the way there.

## License

MIT. See [LICENSE](LICENSE).
