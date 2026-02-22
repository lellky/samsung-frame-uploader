"""Image optimization for Samsung The Frame.

Target is 4K landscape (3840x2160) JPEG with low-ish filesize.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

TARGET_W = 3840
TARGET_H = 2160

# Bump when changing image processing so the app can re-generate outputs.
OPTIMIZER_VERSION = 3


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_color(color: str) -> tuple[int, int, int]:
    c = color.strip().lower()
    if c in {"black", "#000", "#000000"}:
        return (0, 0, 0)
    if c in {"white", "#fff", "#ffffff"}:
        return (255, 255, 255)
    if c.startswith("#") and len(c) == 7:
        try:
            return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
        except ValueError:
            return (0, 0, 0)
    return (0, 0, 0)


def extract_exif_datetime_from_path(path: Path) -> str | None:
    """Extract EXIF datetime without doing a full optimize pass."""

    try:
        with Image.open(path) as im:
            return _extract_exif_datetime(im)
    except Exception:  # noqa: BLE001
        return None


def _extract_exif_datetime(im: Image.Image) -> str | None:
    """Best-effort EXIF datetime extraction.

    Returns ISO-like string (YYYY-MM-DDTHH:MM:SS) when possible.
    """

    try:
        exif = im.getexif()
    except Exception:  # noqa: BLE001
        return None

    # Prefer DateTimeOriginal (36867), then DateTimeDigitized (36868), then DateTime (306)
    for tag in (36867, 36868, 306):
        raw = exif.get(tag)
        if not isinstance(raw, str) or not raw.strip():
            continue
        s = raw.strip()
        # Common EXIF format: "YYYY:MM:DD HH:MM:SS"
        try:
            dt = datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:  # noqa: BLE001
            return s

    return None


def optimize_for_frame(
    *,
    source_path: Path,
    optimized_dir: Path,
    jpeg_quality: int = 85,
    resize_mode: str = "pad",
    background_color: str = "black",
) -> tuple[Path, str, str | None]:
    """Optimize an image and return (optimized_path, sha256, exif_datetime).

    resize_mode:
    - "pad": preserve aspect ratio and add borders to reach 3840x2160
    - "crop": cover + center-crop to 3840x2160

    The output file name is deterministic: <sha256>.jpg.
    """

    sha = sha256_file(source_path)

    # Include processing settings/version in output name so changes can re-generate files.
    variant_key = f"v{OPTIMIZER_VERSION}|{resize_mode}|{background_color}|{TARGET_W}x{TARGET_H}"
    variant_id = hashlib.sha256((sha + "|" + variant_key).encode("utf-8")).hexdigest()[:12]

    optimized_dir.mkdir(parents=True, exist_ok=True)
    out_path = optimized_dir / f"{sha}_{variant_id}.jpg"
    if out_path.exists():
        # exif_datetime is only used for ordering; return best-effort quickly.
        exif_dt = extract_exif_datetime_from_path(source_path)
        return out_path, sha, exif_dt

    with Image.open(source_path) as im:
        exif_datetime = _extract_exif_datetime(im)
        im = ImageOps.exif_transpose(im)

        # Convert to RGB for JPEG; handle alpha by compositing.
        if im.mode == "P" and "transparency" in im.info:
            im = im.convert("RGBA")

        bg_rgb = _parse_color(background_color)

        if im.mode in {"RGBA", "LA"}:
            bg = Image.new("RGB", im.size, bg_rgb)
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")

        mode = resize_mode.strip().lower()

        # Smart behaviour: when padding, many landscape aspect ratios will create
        # black bars (either left/right for 3:2, or top/bottom for panoramas).
        # If the image is landscape and has enough resolution for true 4K output,
        # crop instead to get full-bleed.
        if mode == "pad":
            w0, h0 = im.size
            target_ar = TARGET_W / TARGET_H
            ar = w0 / h0

            if w0 >= TARGET_W and h0 >= TARGET_H and w0 >= h0:
                # Full-bleed crop for high-res landscape images (e.g. 6000x4000).
                if abs(ar - target_ar) > 0.001:
                    mode = "crop"
            else:
                # For smaller images, only force crop for wide panoramas with
                # enough vertical resolution (otherwise padding looks better).
                if h0 >= TARGET_H and ar > target_ar:
                    mode = "crop"

        if mode == "crop":
            # Cover resize to fill 3840x2160, then center crop.
            w, h = im.size
            scale = max(TARGET_W / w, TARGET_H / h)
            new_w = int(round(w * scale))
            new_h = int(round(h * scale))
            im = im.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)

            left = max(0, (new_w - TARGET_W) // 2)
            upper = max(0, (new_h - TARGET_H) // 2)
            right = left + TARGET_W
            lower = upper + TARGET_H
            im = im.crop((left, upper, right, lower))

        else:
            # Pad resize: preserve aspect ratio; add borders.
            w, h = im.size
            scale = min(TARGET_W / w, TARGET_H / h)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            resized = im.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)

            canvas = Image.new("RGB", (TARGET_W, TARGET_H), bg_rgb)
            left = (TARGET_W - new_w) // 2
            upper = (TARGET_H - new_h) // 2
            canvas.paste(resized, (left, upper))
            im = canvas

        im.save(
            out_path,
            format="JPEG",
            quality=jpeg_quality,
            optimize=True,
            progressive=True,
        )

    return out_path, sha, exif_datetime
