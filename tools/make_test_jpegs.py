#!/usr/bin/env python3
"""Generate test JPEGs (and optional matching SVGs) with random squiggles.

Useful for testing uploads, grids, thumbnails, search, and fake camera cards.
Run with the repository's .venv/bin/python:

    ./.venv/bin/python tools/make_test_jpegs.py /tmp/test_photos --count 20
    ./.venv/bin/python tools/make_test_jpegs.py /tmp/fake_card --dcim --count 5 --svg
"""
import argparse
import math
import random
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

PALETTES = [
    ("#f8fafc", ["#ef4444", "#f97316", "#eab308", "#22c55e", "#3b82f6", "#a855f7"]),
    ("#0f172a", ["#38bdf8", "#818cf8", "#f472b6", "#34d399", "#fbbf24", "#fb7185"]),
    ("#fefce8", ["#b91c1c", "#c2410c", "#15803d", "#1d4ed8", "#7e22ce", "#0f766e"]),
    ("#18181b", ["#f43f5e", "#ec4899", "#8b5cf6", "#06b6d4", "#10b981", "#f59e0b"]),
]


def _cubic_bezier_points(p0, p1, p2, p3, steps: int = 24) -> list[tuple[float, float]]:
    pts = []
    for i in range(steps + 1):
        t = i / steps
        u = 1.0 - t
        x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def _random_squiggle(rng: random.Random, width: int, height: int, segments: int = 6):
    """Return (svg_d, poly_points) for a smooth multi-segment cubic Bezier squiggle."""
    margin_x, margin_y = width * 0.08, height * 0.08
    x0 = rng.uniform(margin_x, width - margin_x)
    y0 = rng.uniform(margin_y, height - margin_y)
    d_parts = [f"M {x0:.1f} {y0:.1f}"]
    points: list[tuple[float, float]] = [(x0, y0)]
    cur = (x0, y0)
    angle = rng.uniform(0, 2 * math.pi)
    step_len = min(width, height) * rng.uniform(0.12, 0.28)

    for _ in range(segments):
        angle += rng.uniform(-1.6, 1.6)
        nx = max(margin_x, min(width - margin_x, cur[0] + math.cos(angle) * step_len))
        ny = max(margin_y, min(height - margin_y, cur[1] + math.sin(angle) * step_len))
        c1 = (
            cur[0] + rng.uniform(-step_len * 0.7, step_len * 0.7),
            cur[1] + rng.uniform(-step_len * 0.7, step_len * 0.7),
        )
        c2 = (
            nx + rng.uniform(-step_len * 0.7, step_len * 0.7),
            ny + rng.uniform(-step_len * 0.7, step_len * 0.7),
        )
        nxt = (nx, ny)
        d_parts.append(f"C {c1[0]:.1f} {c1[1]:.1f}, {c2[0]:.1f} {c2[1]:.1f}, {nx:.1f} {ny:.1f}")
        points.extend(_cubic_bezier_points(cur, c1, c2, nxt)[1:])
        cur = nxt

    return " ".join(d_parts), points


def create_squiggle_image(
    jpg_path: Path,
    *,
    width: int,
    height: int,
    rng: random.Random,
    label: str,
    svg_path: Path | None = None,
) -> None:
    bg, colors = rng.choice(PALETTES)
    num_squiggles = rng.randint(5, 11)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    svg_paths = []

    for _ in range(num_squiggles):
        color = rng.choice(colors)
        stroke = rng.randint(max(3, width // 220), max(8, width // 65))
        d_str, pts = _random_squiggle(rng, width, height, segments=rng.randint(4, 9))
        draw.line(pts, fill=color, width=stroke, joint="curve")
        svg_paths.append(
            f'  <path d="{d_str}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    # Subtle label in the corner so test images are easy to tell apart in the grid
    text_color = "#94a3b8" if bg in ("#0f172a", "#18181b") else "#64748b"
    draw.text((24, height - 42), label, fill=text_color, font_size=22)

    jpg_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(jpg_path, format="JPEG", quality=90)

    if svg_path is not None:
        svg_text = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}">\n'
            f'  <rect width="100%" height="100%" fill="{bg}"/>\n'
            + "\n".join(svg_paths)
            + f'\n  <text x="24" y="{height - 20}" fill="{text_color}" '
            f'font-family="sans-serif" font-size="22">{label}</text>\n</svg>\n'
        )
        svg_path.write_text(svg_text, encoding="utf-8")


def parse_size(value: str) -> tuple[int, int]:
    try:
        w, h = value.lower().split("x", 1)
        width, height = int(w), int(h)
        if width < 64 or height < 64:
            raise ValueError
        return width, height
    except ValueError as exc:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT (e.g. 1200x900)") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("destination", nargs="?", type=Path, default=Path("test_jpegs"),
                    help="output directory (default: ./test_jpegs)")
    ap.add_argument("-n", "--count", type=int, default=10, help="number of JPEGs to create (default: 10)")
    ap.add_argument("--size", type=parse_size, default=(1200, 900), help="WIDTHxHEIGHT (default: 1200x900)")
    ap.add_argument("--seed", type=int, default=None, help="random seed (default: random)")
    ap.add_argument("--svg", action="store_true", help="also save a matching .svg file for each JPEG")
    ap.add_argument("--dcim", action="store_true", help="write into DCIM/100MSDCF/ for camera import testing")
    ap.add_argument("--exif", action="store_true", help="tag capture dates/camera via exiftool if installed")
    args = ap.parse_args()

    if args.count < 1:
        ap.error("--count must be at least 1")

    rng = random.Random(args.seed)
    out_dir = args.destination.resolve()
    target_dir = out_dir / "DCIM" / "100MSDCF" if args.dcim else out_dir
    width, height = args.size
    exiftool = shutil.which("exiftool") if args.exif else None
    if args.exif and not exiftool:
        print("note: exiftool not found; writing JPEGs without EXIF tags")

    base_time = datetime(2026, 11, 14, 10, 0, 0)
    for i in range(1, args.count + 1):
        name = f"SQUIGGLE_{i:04d}.JPG"
        jpg_path = target_dir / name
        svg_path = (target_dir / f"SQUIGGLE_{i:04d}.svg") if args.svg else None
        seed_note = f"seed={args.seed}" if args.seed is not None else "random"
        create_squiggle_image(
            jpg_path,
            width=width,
            height=height,
            rng=rng,
            label=f"TEST {i}/{args.count} ({seed_note})",
            svg_path=svg_path,
        )
        if exiftool:
            stamp = (base_time + timedelta(minutes=i)).strftime("%Y:%m:%d %H:%M:%S")
            subprocess.run(
                [exiftool, "-overwrite_original", "-q", "-Make=TestCam", "-Model=Squiggle-1",
                 f"-DateTimeOriginal={stamp}", str(jpg_path)],
                check=True,
            )

    print(f"Created {args.count} JPEG(s)" + (" + SVG(s)" if args.svg else "") + f" in {target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

