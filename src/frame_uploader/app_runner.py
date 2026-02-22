"""Long-running scheduled uploader app.

Defaults to one "tick" every 300 seconds:
- scan input folder for new images
- optimize new images to 3840x2160 JPEG
- upload one optimized image
- prune old uploads on the TV to keep around KEEP_ON_TV items
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from frame_uploader.app_config import AppConfig
from frame_uploader.app_db import AppDB
from frame_uploader.config import TVConfig
from frame_uploader.optimizer import (
    OPTIMIZER_VERSION,
    extract_exif_datetime_from_path,
    optimize_for_frame,
)
from frame_uploader.uploader import (
    connect,
    delete_image,
    get_current,
    list_art,
    select_image,
    upload_image,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def _iter_input_files(cfg: AppConfig) -> list[Path]:
    base = cfg.input_dir
    if not base.exists():
        return []

    it = base.rglob("*") if cfg.recursive_scan else base.glob("*")
    files: list[Path] = []
    for p in it:
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        files.append(p)
    return sorted(files)


def scan_for_new_files(cfg: AppConfig, db: AppDB) -> int:
    count = 0
    seen_paths: set[str] = set()

    for p in _iter_input_files(cfg):
        st = p.stat()
        exif_dt = extract_exif_datetime_from_path(p)
        db.upsert_source_file(
            source_path=p,
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
            exif_datetime=exif_dt,
            desired_optimizer_version=OPTIMIZER_VERSION,
            desired_resize_mode=cfg.resize_mode,
            desired_background_color=cfg.background_color,
        )
        seen_paths.add(str(p))
        count += 1

    # Reconcile: mark removed files as missing so they won't be optimized or uploaded.
    prefix = str(cfg.input_dir)
    if not prefix.endswith("/"):
        prefix += "/"

    for image_id, source_path in db.iter_images_by_source_prefix(prefix):
        if source_path not in seen_paths:
            db.mark_source_missing(image_id=image_id, source_path=source_path)

    return count


def optimize_pending(cfg: AppConfig, db: AppDB, *, limit: int = 25) -> int:
    done = 0

    rows = list(db.iter_pending_optimize(limit=limit))
    if not rows:
        return 0

    seed = _get_order_seed(cfg, db)
    rows.sort(key=lambda r: _order_key(cfg, seed, r))

    for row in rows:
        src = Path(row.source_path)
        if not src.exists():
            db.mark_source_missing(image_id=row.id, source_path=row.source_path)
            continue

        try:
            out_path, sha, exif_dt = optimize_for_frame(
                source_path=src,
                optimized_dir=cfg.optimized_dir,
                jpeg_quality=cfg.jpeg_quality,
                resize_mode=cfg.resize_mode,
                background_color=cfg.background_color,
            )
            db.mark_optimized(
                image_id=row.id,
                sha256=sha,
                optimized_path=out_path,
                exif_datetime=exif_dt or row.exif_datetime,
                optimizer_version=OPTIMIZER_VERSION,
                optimized_resize_mode=cfg.resize_mode,
                optimized_background_color=cfg.background_color,
            )
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("Optimize failed for %s", row.source_path)
            db.mark_optimize_failed(image_id=row.id, error=str(e))

    return done


def _is_my_upload(content_id: str) -> bool:
    return content_id.startswith("MY_") or content_id.startswith("MY-")


def _should_upload_now(cfg: AppConfig, db: AppDB) -> bool:
    if cfg.upload_interval_seconds <= 0:
        return True

    last = db.get_last_uploaded_at()
    if not last:
        return True

    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True

    now = datetime.now(timezone.utc)
    elapsed = (now - last_dt).total_seconds()
    return elapsed >= float(cfg.upload_interval_seconds)


def _get_order_seed(cfg: AppConfig, db: AppDB) -> str | None:
    if cfg.order_mode != "random":
        return None

    if cfg.order_seed:
        return cfg.order_seed

    seed = db.get_state("order_seed")
    if seed:
        return seed

    seed = secrets.token_hex(16)
    db.set_state("order_seed", seed)
    return seed


def _parse_exif_dt(s: str) -> datetime | None:
    try:
        # We store either ISO-like or raw EXIF. Best-effort.
        if "T" in s:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _order_key(cfg: AppConfig, seed: str | None, r) -> tuple:
    mode = cfg.order_mode

    if mode == "filename":
        return (Path(r.source_path).name.casefold(), r.created_at, r.id)

    if mode == "exif_date":
        if r.exif_datetime:
            dt = _parse_exif_dt(r.exif_datetime)
            if dt is not None:
                return (dt.isoformat(), r.created_at, r.id)
        return (r.created_at, r.id)

    if mode == "random":
        s = seed or ""
        token = (s + "|" + (r.source_sha256 or r.source_path)).encode("utf-8")
        h = hashlib.sha256(token).hexdigest()
        return (h, r.created_at, r.id)

    # default: created_at
    return (r.created_at, r.id)


def _next_cycle_image(cfg: AppConfig, db: AppDB) -> tuple[int, str] | None:
    """Return (image_id, optimized_path) for the next image to (re)upload.

    Priority:
    1) any optimized image that has never been uploaded (ordered by ORDER_MODE)
    2) otherwise, cycle through all optimized images in ORDER_MODE and loop

    We persist our position in the DB state table.
    """

    seed = _get_order_seed(cfg, db)

    never = db.list_never_uploaded_images()
    if never:
        never.sort(key=lambda r: _order_key(cfg, seed, r))
        r0 = never[0]
        assert r0.optimized_path
        return r0.id, r0.optimized_path

    rows = db.list_cycle_images()
    if not rows:
        return None

    rows.sort(key=lambda r: _order_key(cfg, seed, r))

    last_raw = db.get_state("cycle_last_image_id")
    start_idx = 0
    if last_raw:
        try:
            last_id = int(last_raw)
            for i, r in enumerate(rows):
                if r.id == last_id:
                    start_idx = (i + 1) % len(rows)
                    break
        except ValueError:
            start_idx = 0

    r = rows[start_idx]
    if not r.optimized_path:
        return None

    return r.id, r.optimized_path


def upload_one(cfg: AppConfig, db: AppDB) -> str | None:
    if not _should_upload_now(cfg, db):
        logger.debug("Upload throttled by UPLOAD_INTERVAL_SECONDS")
        return None

    nxt = _next_cycle_image(cfg, db)
    if nxt is None:
        return None

    image_id, optimized_path = nxt

    tv_cfg = TVConfig.load()
    tv = connect(tv_cfg)

    matte = cfg.matte or tv_cfg.matte

    try:
        if cfg.dry_run:
            logger.info("DRY_RUN: would upload %s", optimized_path)
            return None

        content_id = upload_image(tv, Path(optimized_path), matte=matte)
        db.mark_uploaded(image_id=image_id, tv_content_id=content_id)
        db.set_state("cycle_last_image_id", str(image_id))
        logger.info("Uploaded %s -> %s", optimized_path, content_id)

        if cfg.select_after_upload:
            select_image(tv, content_id)
            logger.info("Selected %s", content_id)

        return content_id

    except Exception as e:  # noqa: BLE001
        logger.exception("Upload failed for %s", optimized_path)
        db.mark_upload_failed(image_id=image_id, error=str(e))
        return None


def cleanup_tv(cfg: AppConfig, db: AppDB) -> int:
    """Ensure only KEEP_ON_TV images remain (according to cleanup_mode)."""

    if cfg.keep_on_tv < 0:
        return 0

    tv_cfg = TVConfig.load()
    tv = connect(tv_cfg)

    tv_art = list_art(tv)
    tv_items = [a for a in tv_art if isinstance(a, dict) and a.get("content_id")]

    # Best-effort: avoid deleting currently displayed artwork.
    try:
        current = get_current(tv)
        current_id = current.get("content_id") if isinstance(current, dict) else None
    except Exception:  # noqa: BLE001
        current_id = None

    deleted = 0

    if cfg.cleanup_mode == "all_my":
        candidates = [
            a
            for a in tv_items
            if isinstance(a.get("content_id"), str) and _is_my_upload(a["content_id"])
        ]

        def parse_tv_date(item: dict) -> datetime:
            raw = item.get("date")
            if isinstance(raw, str) and raw:
                # Often comes as "YYYY:MM:DD HH:MM:SS".
                try:
                    dt = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            # Fallback: epoch.
            return datetime.fromtimestamp(0, tz=timezone.utc)

        candidates_sorted = sorted(candidates, key=parse_tv_date)
        to_delete = max(0, len(candidates_sorted) - cfg.keep_on_tv)
        for item in candidates_sorted[:to_delete]:
            cid = item["content_id"]
            if current_id and cid == current_id:
                logger.info("Skipping delete of currently displayed art: %s", cid)
                continue

            if cfg.dry_run:
                logger.info("DRY_RUN: would delete %s", cid)
                deleted += 1
                continue

            try:
                delete_image(tv, cid)
                db.mark_upload_tv_deleted(tv_content_id=cid)
                logger.info("Deleted %s", cid)
                deleted += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to delete %s: %s", cid, e)

        return deleted

    # app_only: delete based on our upload history.
    uploads = list(db.iter_uploads_not_deleted())
    to_delete = max(0, len(uploads) - cfg.keep_on_tv)

    for u in uploads[:to_delete]:
        cid = u.tv_content_id
        if current_id and cid == current_id:
            logger.info("Skipping delete of currently displayed art: %s", cid)
            continue

        if cfg.dry_run:
            logger.info("DRY_RUN: would delete %s", cid)
            db.mark_upload_tv_deleted(tv_content_id=cid)
            deleted += 1
            continue

        try:
            delete_image(tv, cid)
            db.mark_upload_tv_deleted(tv_content_id=cid)
            logger.info("Deleted %s", cid)
            deleted += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to delete %s: %s", cid, e)

    return deleted


def run_once(cfg: AppConfig, db: AppDB) -> None:
    seen = scan_for_new_files(cfg, db)
    optimized = optimize_pending(cfg, db)

    uploaded = upload_one(cfg, db)
    deleted = cleanup_tv(cfg, db)

    logger.info(
        "tick: scanned=%s optimized=%s uploaded=%s deleted=%s",
        seen,
        optimized,
        uploaded or "-",
        deleted,
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="frame-uploader-app")
    p.add_argument("--once", action="store_true", help="Run a single tick and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = AppConfig.from_env()
    cfg.input_dir.mkdir(parents=True, exist_ok=True)
    cfg.optimized_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    db = AppDB(cfg.db_path)
    db.init()

    logger.info(
        (
            "Starting: input=%s optimized=%s db=%s interval=%ss "
            "upload_interval=%ss keep_on_tv=%s cleanup_mode=%s"
        ),
        cfg.input_dir,
        cfg.optimized_dir,
        cfg.db_path,
        cfg.interval_seconds,
        cfg.upload_interval_seconds,
        cfg.keep_on_tv,
        cfg.cleanup_mode,
    )

    if args.once:
        run_once(cfg, db)
        return

    while True:
        started = time.time()
        try:
            run_once(cfg, db)
        except Exception:  # noqa: BLE001
            logger.exception("tick failed")

        elapsed = time.time() - started
        sleep_s = max(1.0, float(cfg.interval_seconds) - elapsed)
        time.sleep(sleep_s)
