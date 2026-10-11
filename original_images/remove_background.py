"""Remove border-connected white backgrounds and write size-limited transparent WebP.

Install: python -m pip install -r requirements-images.txt
Run:     python remove_background.py
         python remove_background.py --max-kb 200 --max-dimension 1600

Originals are never modified. Existing results in the output folder are replaced.
Only images directly inside the input directory are processed.
"""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, ImageDraw, features


EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def remove_white_background(image: Image.Image, threshold: int, softness: int) -> Image.Image:
    """Flood from outside the canvas, preserving enclosed white artwork.

    Near-white boundary pixels receive partial alpha, with white contamination
    removed from their RGB values to reduce halos on dark backgrounds.
    """
    rgba = np.array(image.convert("RGBA"))
    rgb = rgba[:, :, :3].astype(np.float32)
    minimum = rgb.min(axis=2)
    candidate = (minimum >= threshold - softness) | (rgba[:, :, 3] == 0)
    padded = np.pad(candidate.astype(np.uint8) * 255, 1, constant_values=255)
    # A copy makes the NumPy-backed image writable for Pillow's flood fill.
    mask = Image.fromarray(padded).copy()
    ImageDraw.floodfill(mask, (0, 0), 128, thresh=0)
    connected = np.asarray(mask)[1:-1, 1:-1] == 128

    alpha = np.ones(minimum.shape, dtype=np.float32)
    alpha[connected] = np.clip((threshold - minimum[connected]) / softness, 0, 1)
    partial = (alpha > 0) & (alpha < 1)
    rgb[partial] = np.clip(
        (rgb[partial] - 255 * (1 - alpha[partial, None])) / alpha[partial, None],
        0,
        255,
    )
    rgba[:, :, :3] = np.rint(rgb).astype(np.uint8)
    rgba[:, :, 3] = np.rint(rgba[:, :, 3] * alpha).astype(np.uint8)
    rgba[rgba[:, :, 3] == 0, :3] = 0
    return Image.fromarray(rgba)


def encode_webp(image: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=quality, method=4)
    return buffer.getvalue()


def compress(image: Image.Image, max_bytes: int, max_dimension: int) -> tuple[bytes, tuple[int, int], int]:
    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    while True:
        best = encode_webp(image, 85)
        if len(best) <= max_bytes:
            return best, image.size, 85

        best = encode_webp(image, 60)
        if len(best) <= max_bytes:
            best_quality = 60
            low, high = 61, 84
            while low <= high:
                quality = (low + high) // 2
                trial = encode_webp(image, quality)
                if len(trial) <= max_bytes:
                    best, best_quality = trial, quality
                    low = quality + 1
                else:
                    high = quality - 1
            return best, image.size, best_quality

        if image.size == (1, 1):
            raise ValueError("Size limit is too small for a WebP image")
        size = tuple(max(1, int(dimension * 0.85)) for dimension in image.size)
        image = image.resize(size, Image.Resampling.LANCZOS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path, help="Default: genre_images")
    parser.add_argument("--max-kb", type=int, default=200, help="Maximum bytes / 1000 (default: 200)")
    parser.add_argument("--max-dimension", type=int, default=1280, help="Maximum width/height (default: 1280)")
    parser.add_argument("--white-threshold", type=int, default=230, help="White cutoff, 1-255 (default: 230)")
    parser.add_argument("--softness", type=int, default=20, help="Near-white edge transition (default: 20)")
    args = parser.parse_args()
    if args.max_kb < 1 or args.max_dimension < 1:
        parser.error("--max-kb and --max-dimension must be positive")
    if not 1 <= args.softness <= args.white_threshold <= 255:
        parser.error("Require 1 <= softness <= white-threshold <= 255")
    source = args.input.resolve()
    destination = (args.output or source / "../genre_images").resolve()
    if not source.is_dir():
        parser.error(f"Input directory does not exist: {source}")
    if source == destination:
        parser.error("Output must differ from input to protect originals")
    if not features.check("webp"):
        parser.error("This Pillow installation does not support WebP")
    files = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS)
    if not files:
        print(f"No supported images found in {source}")
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    errors = 0
    used_names: set[str] = set()
    for index, path in enumerate(files, 1):
        name = f"{path.stem}.webp"
        suffix = 1
        while name.casefold() in used_names:
            name = f"{path.stem}_{suffix}.webp"
            suffix += 1
        used_names.add(name.casefold())
        try:
            with Image.open(path) as original:
                image = remove_white_background(
                    ImageOps.exif_transpose(original), args.white_threshold, args.softness
                )
            data, size, quality = compress(image, args.max_kb * 1000, args.max_dimension)
            target = destination / name
            # Resolve symlinks too, so an output file cannot point at an original.
            if target.resolve() in files:
                raise ValueError("Output path points at an original image")
            target.write_bytes(data)
            print(f"[{index}/{len(files)}] {name}: {len(data) / 1000:.1f} KB, {size[0]}x{size[1]}, quality {quality}", flush=True)
        except Exception as exc:
            errors += 1
            print(f"ERROR: {path.name}: {exc}", flush=True)
    print(f"Saved {len(files) - errors}/{len(files)} images to {destination}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
