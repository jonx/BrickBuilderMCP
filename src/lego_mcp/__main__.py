"""Entry point for `lego-mcp` / `python -m lego_mcp`.

Subcommands:
    (no args)            start the MCP server on stdio
    install-library      download the LDraw parts library (~85 MB) to ~/Library/ldraw
    render <file> ...    render an .ldr / .mpd file to PNG (see `render -h`)
    bom <file> ...       orderable parts list for a model (see `bom -h`)
    instructions <file>  step-by-step build order (see `instructions -h`)
    -h | --help          print this help
"""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in {"-h", "--help"}:
        print(__doc__)
        return
    if args and args[0] == "install-library":
        from lego_mcp.parts import install_library
        install_library()
        return
    if args and args[0] == "render":
        from lego_mcp.cli_render import render_cmd
        sys.exit(render_cmd(args[1:]))
    if args and args[0] == "bom":
        from lego_mcp.cli_bom import bom_cmd
        sys.exit(bom_cmd(args[1:]))
    if args and args[0] == "instructions":
        from lego_mcp.cli_bom import instructions_cmd
        sys.exit(instructions_cmd(args[1:]))
    from lego_mcp.server import run
    run()


if __name__ == "__main__":
    main()
