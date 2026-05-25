# LegoMCP — Architecture and Decision Log

## Architecture at a glance

- MCP server, Python, stdio transport, FastMCP API.
- One file does the work: `server.py` holds model state + all tools.
- LDraw is the source of truth. Internal state is just a list of `(part_id, color, x, y, z, rotation)`. Export is `.ldr` / `.mpd`.
- Built-in catalog of ~30 common bricks so the server works out of the box. `lego-mcp install-library` downloads the full LDraw library (~85 MB) for users who want more.
- No GUI automation. LDraw viewers open the exported file.

## Key decisions

### Keep it in one file until it hurts
First pass is single-module on purpose. Splitting into geometry / ldraw / model / validate / server modules is what I'd do for the v2; for v1 it's premature decomposition. ~300–500 lines is fine to read top-to-bottom.

### Built-in catalog, optional full library
85 MB on first run is a friction tax. The built-in set covers basic bricks/plates/tiles/slopes — enough for the LLM to build something fun on day one. The full library is one command away when needed.

### Six canonical rotations, not raw matrices
LDraw stores arbitrary 3×3 matrices, but LEGO snaps to 90°. Exposing full SO(3) to an LLM means half the tool calls produce visually broken models. We expose six names: `identity`, `rot90y`, `rot180y`, `rot270y`, `rot90x`, `rot90z`. Add a matrix escape hatch later if needed.

### AABB collision only
Real geometry intersection means parsing primitive geometry in `.dat` files. Big project. AABB catches gross errors and is enough to keep the LLM honest. Known limitation: tiles on studs may register as colliding because AABBs include stud height.

### Coordinate convention
LDraw: right-handed, **-Y is up**, 1 brick = 20 LDU wide × 24 LDU tall. We don't flip anything for the LLM — every docstring spells it out.

## With more time

- Real stud-clutch validation from `.dat` connection geometry.
- Subassembly / MPD-block authoring tools.
- `render_model` tool shelling to LDView.
- Friendly color name → LDraw ID lookup.
- A small agent script (planner / builder / critic) driving Claude against this server.

## Things to discuss in the walkthrough

- Is 6 rotations the right ceiling, or jump straight to matrices?
- Should `validate_model` block mutations on collision, or always allow + report?
- Built-in catalog: which 30 parts? Currently leaning toward classic City-set bricks.
