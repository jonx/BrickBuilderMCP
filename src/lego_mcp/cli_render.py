"""`lego-mcp render <file>` — one-shot render of an LDraw .ldr/.mpd to PNG.

Built so you can preview any model file without spinning up the MCP server or
opening Claude Desktop. Wraps `import_ldr` + `render_model_png` plus an
optional watermark overlay.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


COLOR_MODES = ("model", "instance", "row", "rotation")
WATERMARK_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")


def _parse_hex_color(s: str) -> tuple[int, int, int]:
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise argparse.ArgumentTypeError(f"expected hex color like #f5f5f8, got {s!r}")
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid hex color {s!r}") from e


def _load_font(size: int) -> ImageFont.ImageFont:
    """Try a few common system fonts before falling back to PIL's default."""
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_watermark(png_bytes: bytes, text: str, position: str,
                    color: tuple[int, int, int], opacity: int,
                    size: int, margin: int) -> bytes:
    """Overlay `text` on a rendered PNG. Returns new PNG bytes."""
    import io

    base = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(size)

    # Measure the text. textbbox is the modern PIL API.
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    W, H = base.size

    if position == "top-left":
        xy = (margin, margin)
    elif position == "top-right":
        xy = (W - tw - margin, margin)
    elif position == "bottom-left":
        xy = (margin, H - th - margin * 2)
    else:  # bottom-right
        xy = (W - tw - margin, H - th - margin * 2)

    fill = (color[0], color[1], color[2], max(0, min(255, opacity)))
    draw.text(xy, text, fill=fill, font=font)
    merged = Image.alpha_composite(base, overlay).convert("RGB")

    out = io.BytesIO()
    merged.save(out, "PNG", optimize=True)
    return out.getvalue()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lego-mcp render",
        description=("Render an LDraw .ldr / .mpd model to a PNG image using "
                     "LegoMCP's built-in isometric renderer."),
    )
    p.add_argument("input", type=Path,
                   help="Path to an .ldr or .mpd file.")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="Output PNG path. Default: <input-stem>.png in the input's directory.")
    p.add_argument("-w", "--width", type=int, default=1200,
                   help="Image width in pixels. Default: 1200.")
    p.add_argument("-H", "--height", type=int, default=900,
                   help="Image height in pixels. Default: 900.")
    p.add_argument("--color-mode", choices=COLOR_MODES, default="model",
                   help=("'model' = actual part colors (default); 'instance' = one "
                         "color per piece; 'row' = color by brick course; "
                         "'rotation' = color by orientation."))
    p.add_argument("--hidden-edges", action="store_true",
                   help="Draw fully-covered contact faces as dotted lines.")
    p.add_argument("--view-angle", type=float, default=0.0, metavar="DEG",
                   help=("Camera azimuth in degrees, rotating around the world "
                         "Y-axis. 0 = default iso view; try 45, 90, 135, 180, "
                         "etc. to spin around the model. Default: 0."))
    p.add_argument("--turntable", type=int, default=None, metavar="N",
                   help=("Instead of one image, write N renders sweeping a full "
                         "360°. With --output set, the path gets a _NNN suffix "
                         "before .png; otherwise frames go next to the input as "
                         "<input-stem>_NNN.png. Overrides --view-angle."))
    p.add_argument("--background", type=_parse_hex_color, default=(245, 245, 248),
                   metavar="#RRGGBB",
                   help="Background color as hex. Default: #f5f5f8.")
    p.add_argument("--watermark", type=str, default=None, metavar="TEXT",
                   help="Overlay this text on the rendered image.")
    p.add_argument("--watermark-position", choices=WATERMARK_POSITIONS,
                   default="bottom-right",
                   help="Where to draw the watermark. Default: bottom-right.")
    p.add_argument("--watermark-color", type=_parse_hex_color, default=(40, 40, 40),
                   metavar="#RRGGBB",
                   help="Watermark color. Default: #282828 (dark gray).")
    p.add_argument("--watermark-opacity", type=int, default=140, metavar="0-255",
                   help="Watermark alpha (0 transparent .. 255 opaque). Default: 140.")
    p.add_argument("--watermark-size", type=int, default=18,
                   help="Watermark font size in px. Default: 18.")
    p.add_argument("--watermark-margin", type=int, default=14,
                   help="Watermark distance from image edges, in px. Default: 14.")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="Suppress informational stdout output.")
    return p


def render_cmd(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    input_path: Path = args.input.expanduser().resolve()
    if not input_path.is_file():
        print(f"error: input file not found: {input_path}", file=sys.stderr)
        return 2

    output_path: Path = (
        args.output.expanduser().resolve() if args.output
        else input_path.with_suffix(".png")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    from lego_mcp import server
    from lego_mcp.render import render_model_png

    if not args.quiet:
        print(f"loading library (one-time index)...")
    server._ensure_library_loaded()

    if not args.quiet:
        print(f"importing {input_path.name} ...")
    result = server.import_ldr(str(input_path))
    if not args.quiet:
        print(f"  loaded {result['loaded']} parts "
              f"({result.get('known_parts', 0)} known, "
              f"{result.get('unknown_parts', 0)} unknown)")

    def render_at(angle: float) -> bytes:
        if not args.quiet:
            print(f"rendering {args.width}x{args.height} @ {angle:.0f}° ...")
        png = render_model_png(
            server.STATE.parts, server.PART_INDEX,
            width=args.width, height=args.height,
            background=args.background,
            color_mode=args.color_mode,
            hidden_edges=args.hidden_edges,
            view_angle=angle,
        )
        if args.watermark:
            png = _draw_watermark(
                png, args.watermark,
                position=args.watermark_position,
                color=args.watermark_color,
                opacity=args.watermark_opacity,
                size=args.watermark_size,
                margin=args.watermark_margin,
            )
        return png

    if args.turntable and args.turntable > 0:
        for i in range(args.turntable):
            angle = 360.0 * i / args.turntable
            png = render_at(angle)
            stem = output_path.with_suffix("").name
            frame_path = output_path.with_name(f"{stem}_{i:03d}.png")
            frame_path.write_bytes(png)
            if not args.quiet:
                print(f"  wrote {frame_path}  ({len(png) / 1024:.1f} KB)")
        return 0

    png = render_at(args.view_angle)
    output_path.write_bytes(png)
    if not args.quiet:
        kb = len(png) / 1024
        print(f"wrote {output_path}  ({kb:.1f} KB)")
    return 0
