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
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from frame_uploader.app_config import AppConfig
from frame_uploader.app_db import AppDB, PendingSelection, SelectionKind
from frame_uploader.config import TVConfig
from frame_uploader.optimizer import (
    OPTIMIZER_VERSION,
    extract_exif_datetime_from_path,
    optimize_for_frame,
)
from frame_uploader.uploader import (
    ArtModeRequiredError,
    connect,
    delete_image,
    get_current,
    is_tv_reachable,
    list_art,
    select_image,
    upload_image,
)

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


@dataclass(frozen=True)
class NextImage:
    image_id: int
    optimized_path: str
    kind: SelectionKind
    cycle_remaining_ids: list[int] | None = None


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

    times = []
    for raw in (db.get_last_uploaded_at(), db.get_state("rotation_completed_at")):
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw)
            times.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    if not times:
        return True

    now = datetime.now(timezone.utc)
    elapsed = (now - max(times)).total_seconds()
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


def _next_cycle_image(cfg: AppConfig, db: AppDB) -> NextImage | None:
    """Alternate new images with the existing round without moving its cursor."""

    seed = _get_order_seed(cfg, db)

    never = db.list_never_uploaded_images()
    never.sort(key=lambda r: _order_key(cfg, seed, r))

    def next_new() -> NextImage | None:
        if not never:
            return None
        row = never[0]
        assert row.optimized_path
        return NextImage(row.id, row.optimized_path, "new")

    last_kind = db.get_state("rotation_last_kind")
    if never and last_kind != "new":
        return next_new()

    rows = db.list_cycle_images()
    if never:
        never_ids = {row.id for row in never}
        rows = [row for row in rows if row.id not in never_ids]
    if not rows:
        return next_new()

    by_id = {row.id: row for row in rows}
    remaining_raw = db.get_state("cycle_remaining_ids")
    remaining = json.loads(remaining_raw) if remaining_raw else []
    # Removed/unavailable sources no longer consume slots in this round.
    remaining = [image_id for image_id in remaining if image_id in by_id]
    if not remaining:
        # Snapshot one finite round. Images arriving later join a subsequent
        # round instead of extending this one indefinitely.
        rows.sort(key=lambda r: _order_key(cfg, seed, r))
        start_idx = 0
        last_raw = db.get_state("cycle_last_image_id")
        if last_raw:
            try:
                last_id = int(last_raw)
                for i, row in enumerate(rows):
                    if row.id == last_id:
                        start_idx = (i + 1) % len(rows)
                        break
            except ValueError:
                pass
        remaining = [row.id for row in rows[start_idx:] + rows[:start_idx]]

    last_image_id = db.get_state("rotation_last_image_id")
    if last_kind == "new" and (never or len(remaining) > 1):
        remaining = [image_id for image_id in remaining if str(image_id) != last_image_id]
    if remaining:
        row = by_id[remaining[0]]
        assert row.optimized_path
        return NextImage(row.id, row.optimized_path, "cycle", remaining[1:])

    return next_new()


def _complete_pending_selection(cfg: AppConfig, db: AppDB, tv, pending: PendingSelection) -> None:
    if cfg.select_after_upload:
        try:
            select_image(tv, pending.tv_content_id)
        except ArtModeRequiredError as error:
            logger.info("Selection pending; will retry: %s", error)
            return
        except Exception as error:  # noqa: BLE001
            logger.warning("Selection of %s failed; will retry: %s", pending.tv_content_id, error)
            return
    db.complete_selection(pending)


def upload_one(cfg: AppConfig, db: AppDB) -> str | None:
    """Upload one image, or retry an unfinished selection without uploading again."""
    pending = db.get_pending_selection()
    nxt = None
    selection_only = False
    if pending:
        row = db.get_image(pending.image_id)
        if row is None or row.optimized_path is None or row.tv_content_id != pending.tv_content_id:
            if cfg.dry_run:
                logger.info("DRY_RUN: would discard stale pending selection")
                return None
            # The source was removed or replaced. Do not display the stale version.
            db.set_state("pending_selection", None)
            pending = None
        else:
            nxt = NextImage(row.id, row.optimized_path, pending.kind, pending.cycle_remaining_ids)
            selection_only = row.tv_deleted_at is None

    if not selection_only and not _should_upload_now(cfg, db):
        logger.debug("Upload throttled by UPLOAD_INTERVAL_SECONDS")
        return None
    if nxt is None:
        nxt = _next_cycle_image(cfg, db)
    if nxt is None:
        return None

    image_id, optimized_path = nxt.image_id, nxt.optimized_path
    if cfg.dry_run:
        if selection_only:
            logger.info("DRY_RUN: would retry pending selection")
        else:
            logger.info("DRY_RUN: would upload %s", optimized_path)
        return None

    tv_cfg = TVConfig.load()
    if not is_tv_reachable(tv_cfg):
        logger.info("Skipping upload: TV not reachable (%s:%s)", tv_cfg.host, tv_cfg.port)
        return None

    tv = connect(tv_cfg)

    if selection_only:
        assert pending is not None
        _complete_pending_selection(cfg, db, tv, pending)
        return None

    matte = cfg.matte or tv_cfg.matte

    try:
        content_id = upload_image(tv, Path(optimized_path), matte=matte)
    except ArtModeRequiredError as e:
        logger.info("Skipping upload: %s", e)
        return None
    except OSError as e:
        logger.info("Skipping upload: TV not reachable (%s:%s): %s", tv_cfg.host, tv_cfg.port, e)
        return None
    except Exception as e:  # noqa: BLE001
        logger.exception("Upload failed for %s", optimized_path)
        db.mark_upload_failed(image_id=image_id, error=str(e))
        return None

    db.mark_uploaded(
        image_id=image_id,
        tv_content_id=content_id,
        selection_kind=nxt.kind,
        cycle_remaining_ids=nxt.cycle_remaining_ids,
    )
    logger.info("Uploaded %s -> %s", optimized_path, content_id)
    pending = PendingSelection(image_id, content_id, nxt.kind, nxt.cycle_remaining_ids)
    _complete_pending_selection(cfg, db, tv, pending)
    return content_id


def cleanup_tv(cfg: AppConfig, db: AppDB) -> int:
    """Ensure only KEEP_ON_TV images remain (according to cleanup_mode)."""

    if cfg.keep_on_tv < 0:
        return 0

    tv_cfg = TVConfig.load()
    if not is_tv_reachable(tv_cfg):
        logger.info("Skipping cleanup: TV not reachable (%s:%s)", tv_cfg.host, tv_cfg.port)
        return 0

    tv = connect(tv_cfg)

    try:
        tv_art = list_art(tv)
    except OSError as e:
        logger.info("Skipping cleanup: TV not reachable (%s:%s): %s", tv_cfg.host, tv_cfg.port, e)
        return 0
    except Exception as e:  # noqa: BLE001
        logger.warning("Skipping cleanup: failed to list TV art: %s", e)
        return 0
    # Reconciliation needs a complete, valid inventory; a malformed response must
    # not be mistaken for evidence that tracked uploads have disappeared.
    if not isinstance(tv_art, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("content_id"), str)
        or not item["content_id"]
        for item in tv_art
    ):
        logger.warning("Skipping cleanup: invalid TV art list")
        return 0
    tv_items = tv_art

    # Best-effort: avoid deleting currently displayed artwork.
    try:
        current = get_current(tv)
        current_id = current.get("content_id") if isinstance(current, dict) else None
    except Exception:  # noqa: BLE001
        current_id = None

    pending = db.get_pending_selection()
    pending_id = pending.tv_content_id if pending else None
    protected_ids = {cid for cid in (current_id, pending_id) if cid}
    if pending_id and pending_id != current_id:
        tv_ids = {item["content_id"] for item in tv_items}
        if pending_id not in tv_ids and not cfg.dry_run:
            # A pending upload deleted outside the app must be uploaded again.
            db.mark_upload_tv_deleted(tv_content_id=pending_id)

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
            if cid in protected_ids:
                logger.info("Skipping delete of displayed or pending art: %s", cid)
                continue

            if cfg.dry_run:
                logger.info("DRY_RUN: would delete %s", cid)
                deleted += 1
                continue

            try:
                delete_image(tv, cid)
                db.mark_upload_tv_deleted(tv_content_id=cid)
                deleted += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to delete %s: %s", cid, e)

        return deleted

    # app_only: reconcile our history before counting retention candidates. Images
    # removed outside this app must not be retried on every tick or inflate the count.
    tv_ids = {item["content_id"] for item in tv_items}
    uploads = []
    for upload in db.iter_uploads_not_deleted():
        cid = upload.tv_content_id
        if cid in tv_ids or cid == current_id:
            uploads.append(upload)
        elif cfg.dry_run:
            logger.info("DRY_RUN: would mark upload absent from TV: %s", cid)
        else:
            db.mark_upload_tv_deleted(tv_content_id=cid)
            logger.info("Marked upload absent from TV: %s", cid)

    to_delete = max(0, len(uploads) - cfg.keep_on_tv)

    for u in uploads[:to_delete]:
        cid = u.tv_content_id
        if cid in protected_ids:
            logger.info("Skipping delete of displayed or pending art: %s", cid)
            continue

        if cfg.dry_run:
            logger.info("DRY_RUN: would delete %s", cid)
            deleted += 1
            continue

        try:
            delete_image(tv, cid)
            db.mark_upload_tv_deleted(tv_content_id=cid)
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
