"""Entry point for `lego-mcp` / `python -m lego_mcp`.

Subcommands:
    (no args)          start the MCP server on stdio
    install-library    download the LDraw parts library (~85 MB) to ~/Library/ldraw
    -h | --help        print this help
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
    from lego_mcp.server import run
    run()


if __name__ == "__main__":
    main()
