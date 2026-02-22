"""Configuration for the scheduled Frame uploader app.

The service is intended to run headless (often in Docker) and is configured primarily
via environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw).expanduser()


@dataclass(frozen=True)
class AppConfig:
    input_dir: Path
    optimized_dir: Path
    db_path: Path

    # How often the loop runs (scan/optimize/cleanup cadence)
    interval_seconds: int

    # Throttle: minimum time between successful uploads
    upload_interval_seconds: int

    keep_on_tv: int
    jpeg_quality: int
    matte: str | None
    select_after_upload: bool
    cleanup_mode: str
    recursive_scan: bool

    # Upload/cycle ordering
    # - created_at: order by when first seen by the app
    # - filename: order by source filename (case-insensitive)
    # - exif_date: order by EXIF DateTimeOriginal (falls back to created_at)
    # - random: stable pseudo-random order based on ORDER_SEED
    order_mode: str
    order_seed: str | None

    # Image resize behavior:
    # - pad: preserve aspect ratio; add borders to reach 3840x2160
    # - crop: cover + center-crop to 3840x2160
    resize_mode: str

    # Used both for alpha compositing (RGBA->JPEG) and padding borders.
    background_color: str

    dry_run: bool

    @classmethod
    def from_env(cls) -> "AppConfig":
        input_dir = _env_path("INPUT_DIR", Path("/data/incoming"))
        optimized_dir = _env_path("OPTIMIZED_DIR", Path("/data/optimized"))
        db_path = _env_path("DB_PATH", Path("/data/db/frame_uploader.sqlite3"))

        interval_seconds = _env_int("INTERVAL_SECONDS", 300)
        upload_interval_seconds = _env_int("UPLOAD_INTERVAL_SECONDS", 300)

        keep_on_tv = _env_int("KEEP_ON_TV", 20)
        jpeg_quality = _env_int("JPEG_QUALITY", 85)

        # Matte controls the TV's passepartout/frame. Use "none" for full-bleed.
        matte = os.getenv("MATTE")
        if matte is None:
            matte = "none"
        elif not matte.strip():
            matte = "none"

        select_after_upload = _env_bool("SELECT_AFTER_UPLOAD", True)

        # cleanup_mode:
        # - app_only: delete only content_ids that were uploaded by this app (tracked in SQLite)
        # - all_my: delete any user-uploaded art on the TV (MY_* / MY-*)
        cleanup_mode = os.getenv("CLEANUP_MODE", "all_my").strip().lower()

        recursive_scan = _env_bool("RECURSIVE_SCAN", True)

        order_mode = os.getenv("ORDER_MODE", "created_at").strip().lower()
        order_seed = os.getenv("ORDER_SEED")
        if order_seed is not None and not order_seed.strip():
            order_seed = None

        resize_mode = os.getenv("RESIZE_MODE", "pad").strip().lower()

        background_color = os.getenv("BACKGROUND_COLOR", "black").strip().lower()

        dry_run = _env_bool("DRY_RUN", False)

        return cls(
            input_dir=input_dir,
            optimized_dir=optimized_dir,
            db_path=db_path,
            interval_seconds=interval_seconds,
            upload_interval_seconds=upload_interval_seconds,
            keep_on_tv=keep_on_tv,
            jpeg_quality=jpeg_quality,
            matte=matte,
            select_after_upload=select_after_upload,
            cleanup_mode=cleanup_mode,
            recursive_scan=recursive_scan,
            order_mode=order_mode,
            order_seed=order_seed,
            resize_mode=resize_mode,
            background_color=background_color,
            dry_run=dry_run,
        )
