# LegoMCP

An [MCP](https://modelcontextprotocol.io) server that lets an LLM design **buildable** LEGO models — by emitting validated LDraw files, not by clicking around in a CAD app.

Drop it into Claude Desktop (or any MCP client), say *"build me a small red house"*, and the LLM gets a set of semantic tools that map to real LEGO operations: `add_part`, `build_wall`, `build_room`, `mirror_subassembly`, `validate_model`, `render_model`, undo/redo, checkpoints, persistent projects. The model state lives in the server. The output is a real `.ldr` / `.mpd` file you can open in [BrickLink Studio](https://www.bricklink.com/v3/studio/download.page), [LeoCAD](https://www.leocad.org/), or any LDraw viewer.

> **Status**: alpha but functional end-to-end. 50 tests passing, real LEGO geometry (12,800-part catalog after `install-library`), buildability checks (no floating bricks, no collisions, no unanchored islands), built-in isometric renderer with stud detail, multi-block MPD export, and an optional standalone agent client that drives Claude over the same MCP server.

## Why this exists

The naive approach to LLM-built LEGO is to let the model drive a CAD app via screen automation. That fails — too many micro-actions, no spatial ground truth, errors compound. LegoMCP gives the LLM a *semantic* API where every action is a typed function call, every change is validated against real LEGO connection rules, every state is reversible.

The CAD app becomes the viewer. The MCP server is the source of truth.

## The non-negotiable rules

The toolchain enforces these so the output is actually buildable:

1. **Connection rule**: every brick must have at least one stud's worth (≥ 400 LDU² of XZ overlap) with a baseplate / brick below it, OR be connected from above (for SNOT / hanging bricks). Anything else is reported as `floating`.
2. **Anchored rule**: connected islands must reach the ground via a chain of connections. A floating tower whose bricks all touch each other but the whole thing levitates is reported as `unanchored`.
3. **No overlap**: bricks must not occupy the same space. `add_part(strict=True)` rejects overlapping placements at insertion time.
4. **Real geometry**: AABBs, stud positions, and dimensions come from the actual LDraw `.dat` files (recursively parsed through subparts and `stug-NxN` stud-group primitives). A "Brick 2x4" really is 80 LDU × 40 LDU × 24 LDU with 8 studs at the correct positions.

## Quick start

```bash
# 1. uv (manages Python + deps)
brew install uv                                   # macOS
# or: curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install
git clone <this repo> LegoMCP && cd LegoMCP
uv tool install --from . lego-mcp

# 3. Download the full LDraw parts library (~135 MB, one-time, takes a few minutes)
lego-mcp install-library

# 4. Point Claude Desktop at the server. In
#    ~/Library/Application Support/Claude/claude_desktop_config.json :
{
  "mcpServers": {
    "lego": { "command": "lego-mcp" }
  }
}
```

Restart Claude Desktop. Then in the chat: *"List the parts in your current LEGO model. Now build me a small red house on a tan baseplate."*

You'll find PNG renders in `./renders/<timestamp>.png` (history preserved; `latest.png` is the most recent).

## What the LLM can do

### Core tools
| Tool | What it does |
|---|---|
| `create_model(name)` | Start a fresh model |
| `add_part(part_id, color, x, y, z, rotation, strict=False)` | Place a brick. `strict=True` rejects overlap/floating placements. |
| `remove_part(instance_id)`, `move_part`, `rotate_part` | Edit |
| `list_parts(limit, subassembly)` | Inspect, optionally filtered |
| `search_parts(query)` | Find IDs by name ("arch 1x6", "window 2x4 with pane") |
| `get_part_info(part_id)` | Dimensions, stud positions, AABB |
| `list_colors()` | The ~22 named colors with LDraw IDs |
| `validate_model()` | Reports collisions, unknown parts, floating, unanchored islands |
| `render_model(width, height, color_mode, hidden_edges)` | Built-in isometric PNG to `./renders/<timestamp>.png`. `color_mode` can be `model`, `instance`, `row`, or `rotation`; hidden/internal edges render dotted while exposed edges stay solid. |
| `export_ldr(path)` / `export_mpd(path)` / `import_ldr(path)` | LDraw I/O |
| `undo()` / `redo()` | O(1) operation-based |
| `save_checkpoint(name)` / `restore_checkpoint(name)` / `list_checkpoints()` | Named in-memory snapshots |

### Subassembly tools
| Tool | What it does |
|---|---|
| `set_current_subassembly(name)` | Tag subsequent additions with a group name |
| `list_subassemblies()` | Names + part counts |
| `remove_subassembly(name)` | Delete every part with the tag |
| `clone_subassembly(src, dst, x_offset, y_offset, z_offset)` | Duplicate a built component to a new location |
| `mirror_subassembly(src, dst, axis, plane_offset)` | Bilateral symmetry — build half a cathedral, mirror it |
| `move_subassembly(name, dx, dy, dz)` | Move a rigid tagged module, undoable per part |
| `analyze_assembly_ports(subassembly)` | Cluster exposed studs/receivers into usable attachment ports for a whole module |
| `find_subassembly_connections(movable, target)` | Suggest offsets that align exposed ports between modules |
| `plan_build_sequence(subassembly, max_steps, start_after)` | Turn the current target model into human-style build steps where every step has support below. Page through large models with `start_after`. |
| `next_build_step(subassembly, built_count)` | Return the next physically placeable piece after a built prefix. |

### High-level building helpers
| Tool | What it does |
|---|---|
| `build_wall(x0, z0, x1, z1, height_rows, color, bond, brick_part, base_y, inset_ends)` | A straight wall with `stretcher` or `running` bond. Each running-bond row is offset by half a brick — real masonry. |
| `build_perimeter(points, height_rows, color, base_y, thickness_studs, palette)` | Generic bonded rectilinear wall outline from outer-corner points. This is the footprint compiler for plans/images/models; `build_room` is a rectangle wrapper around it. |
| `build_room(x_min, z_min, x_max, z_max, height_rows, color, base_y, palette)` | A 2-stud-thick rectangular perimeter with bonded corners. Courses alternate which wall direction owns each corner, so the next row bridges the seam below. Use `palette=["3001"]` to force 2x4-only walls when dimensions fit. |
| `build_wall_with_openings(start_x, start_z, end_x, end_z, height_rows, openings, ...)` | Straight wall compiler with rectangular, round-arch, or lancet window spans. Openings can be transparent/colored fill or true voids. |
| `build_stepped_gable_roof(...)` / `build_stepped_pyramid_roof(...)` | Connector-aware stepped roofs for naves and towers. Layers overlap so validation sees real support. |
| `build_floor(x_min, z_min, x_max, z_max, y, color, part_id)` | Tile a rectangle with plates |
| `repeat_pattern(part_id, count, dx, dy, dz, ...)` | Array of identical parts |

### Persistent state & multimodal context
| Tool | What it does |
|---|---|
| `save_project(name)` / `load_project(name)` / `list_projects()` | Disk-persisted projects (multi-block MPD + JSON notes) |
| `add_note(key, text)` / `get_note(key)` / `list_notes()` / `remove_note(key)` | Sticky observations that survive turns — designed for "Claude looks at architectural plans and remembers measurements" |

### Prompts (user-invokable from Claude Desktop's slash menu)
- `build` — start a fresh build with a goal
- `from_plans` — multimodal cathedral-style build: upload plans, decompose into subassemblies, build half + mirror
- `from_image` — build from a reference photo
- `rescue` — load an existing model and clean it up
- `techniques` — print the LEGO techniques cheat sheet

### Resources (read-only references)
- `lego://techniques` — running/English bond, SNOT, MILS, mirror-for-symmetry, etc.
- `lego://coords` — LDraw coordinate convention reference
- `lego://workflow` — the build-validate-render loop
- `lego://model/current` — live state of the current model (JSON)

## Rotation reference

Six canonical orientations (LEGO almost always snaps to 90°):

| Name | Effect |
|---|---|
| `identity` | No rotation |
| `rot90y`, `rot180y`, `rot270y` | Rotate around Y (vertical) |
| `rot90x` | Rotate around X (tips a brick on its side) |
| `rot90z` | Rotate around Z |

## Coordinate convention (LDraw)

- Right-handed, **-Y is up.**
- 1 stud = **20 LDU** wide. 1 plate = **8 LDU** tall. 1 brick = **24 LDU** tall (= 3 plates).
- Part origins are at the **center of the bottom face.**
- A baseplate (3811) sits at y=0; its top face is at y=-4. A brick at y=-4 sits on the baseplate.
- Stack a brick on top of another: subtract 24 from y.

## Run as a standalone agent (without Claude Desktop)

```bash
uv sync --extra agent
export ANTHROPIC_API_KEY=sk-ant-...
uv run python -m lego_mcp.agent "build me a small red house on a tan baseplate"
```

The agent spawns `lego-mcp` as a subprocess, fetches the tool list over MCP JSON-RPC, hands the tools to Claude via the Anthropic SDK, and runs the tool-use loop until Claude stops calling tools. Final model written to `./agent_build.mpd` and rendered.

## Development

```bash
uv sync --extra dev --extra agent
uv run pytest         # 50 tests, including a real MCP stdio handshake + buildability checks
```

Architecture and decisions in [NOTES.md](NOTES.md).

## What this is *not* yet

- **No physics simulation.** A connected structure that would topple is reported as valid.
- **No stud-clutch geometry.** Connection is approximated by axis-aligned XZ overlap, not by per-stud mating. False negatives can happen with rotated parts or 1x1 corner contacts.
- **The volume compiler is still rectilinear.** `build_perimeter(points=...)` handles closed orthogonal footprints, and straight walls can now have arched/lancet openings. Diagonal/curved walls, integrated openings across arbitrary polygons, and organic surfaces still need richer volume compilation.
- **Generic helpers don't know about half-stud / jumper-plate offsets.** Vertical 8-LDU plate offsets work; horizontal half-stud needs manual placement.
- **No photoreal renderer.** Built-in is isometric AABB-with-studs; for pretty renders, open the exported MPD in BrickLink Studio.

The [Strasbourg Cathedral astronomical clock](https://en.wikipedia.org/wiki/Strasbourg_astronomical_clock) is the north star — the toolchain has the seams (subassemblies, mirror, multi-block MPD, persistent projects with notes) for the LLM to grow into that. NOTES.md tracks the gap.

## License

MIT. See [LICENSE](LICENSE).
