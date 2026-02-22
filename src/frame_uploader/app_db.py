"""SQLite-backed state store for the scheduled uploader app."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class ImageRow:
    id: int
    source_path: str
    source_mtime_ns: int
    source_size: int
    source_sha256: str | None
    optimized_path: str | None
    exif_datetime: str | None
    optimizer_version: int | None
    optimized_resize_mode: str | None
    optimized_background_color: str | None
    status: str
    last_error: str | None
    created_at: str
    optimized_at: str | None
    uploaded_at: str | None
    tv_content_id: str | None
    tv_deleted_at: str | None


@dataclass(frozen=True)
class UploadRow:
    id: int
    image_id: int
    tv_content_id: str
    uploaded_at: str
    tv_deleted_at: str | None


class AppDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def iter_images_by_source_prefix(self, prefix: str) -> Iterable[tuple[int, str]]:
        """Iterate (id, source_path) for images whose source_path starts with prefix."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, source_path FROM images WHERE source_path LIKE ?",
                (prefix + "%",),
            ).fetchall()
        for r in rows:
            yield int(r["id"]), str(r["source_path"])

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY,
                    source_path TEXT NOT NULL UNIQUE,
                    source_mtime_ns INTEGER NOT NULL,
                    source_size INTEGER NOT NULL,
                    source_sha256 TEXT,
                    optimized_path TEXT,
                    exif_datetime TEXT,
                    optimizer_version INTEGER,
                    optimized_resize_mode TEXT,
                    optimized_background_color TEXT,
                    status TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    optimized_at TEXT,
                    uploaded_at TEXT,
                    tv_content_id TEXT,
                    tv_deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_images_status_created_at
                    ON images(status, created_at);

                CREATE INDEX IF NOT EXISTS idx_images_uploaded_at
                    ON images(uploaded_at);

                CREATE INDEX IF NOT EXISTS idx_images_tv_content_id
                    ON images(tv_content_id);

                CREATE TABLE IF NOT EXISTS uploads (
                    id INTEGER PRIMARY KEY,
                    image_id INTEGER NOT NULL,
                    tv_content_id TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    tv_deleted_at TEXT,
                    FOREIGN KEY(image_id) REFERENCES images(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_uploads_uploaded_at
                    ON uploads(uploaded_at);

                CREATE INDEX IF NOT EXISTS idx_uploads_tv_content_id
                    ON uploads(tv_content_id);

                CREATE INDEX IF NOT EXISTS idx_uploads_image_id
                    ON uploads(image_id);

                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

            # --- lightweight migrations for existing DBs ---
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
            if "exif_datetime" not in cols:
                conn.execute("ALTER TABLE images ADD COLUMN exif_datetime TEXT")
            if "optimizer_version" not in cols:
                conn.execute("ALTER TABLE images ADD COLUMN optimizer_version INTEGER")
            if "optimized_resize_mode" not in cols:
                conn.execute("ALTER TABLE images ADD COLUMN optimized_resize_mode TEXT")
            if "optimized_background_color" not in cols:
                conn.execute("ALTER TABLE images ADD COLUMN optimized_background_color TEXT")

    def upsert_source_file(
        self,
        *,
        source_path: Path,
        mtime_ns: int,
        size: int,
        exif_datetime: str | None,
        desired_optimizer_version: int,
        desired_resize_mode: str,
        desired_background_color: str,
    ) -> None:
        now = _utc_now_iso()
        sp = str(source_path)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id,
                    source_mtime_ns,
                    source_size,
                    exif_datetime,
                    optimized_path,
                    optimizer_version,
                    optimized_resize_mode,
                    optimized_background_color,
                    status
                FROM images
                WHERE source_path = ?
                """,
                (sp,),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO images(
                        source_path,
                        source_mtime_ns,
                        source_size,
                        exif_datetime,
                        status,
                        created_at
                    ) VALUES (?, ?, ?, ?, 'new', ?)
                    """,
                    (sp, mtime_ns, size, exif_datetime, now),
                )
                return

            unchanged = int(row["source_mtime_ns"]) == mtime_ns and int(row["source_size"]) == size
            if unchanged:
                # Backfill EXIF datetime if we didn't have it before.
                if row["exif_datetime"] is None and exif_datetime is not None:
                    conn.execute(
                        "UPDATE images SET exif_datetime = ? WHERE source_path = ?",
                        (exif_datetime, sp),
                    )

                # If optimization settings changed since last optimize, re-optimize.
                has_optimized = row["optimized_path"] is not None
                settings_match = (
                    row["optimizer_version"] == desired_optimizer_version
                    and row["optimized_resize_mode"] == desired_resize_mode
                    and row["optimized_background_color"] == desired_background_color
                )

                if has_optimized and not settings_match:
                    conn.execute(
                        """
                        UPDATE images
                        SET optimized_path = NULL,
                            optimizer_version = NULL,
                            optimized_resize_mode = NULL,
                            optimized_background_color = NULL,
                            status = 'new',
                            last_error = NULL,
                            optimized_at = NULL
                        WHERE source_path = ?
                        """,
                        (sp,),
                    )

                return

            # File changed: mark as new and clear downstream fields.
            image_id = int(row["id"])
            conn.execute(
                """
                UPDATE images
                SET source_mtime_ns = ?,
                    source_size = ?,
                    source_sha256 = NULL,
                    optimized_path = NULL,
                    exif_datetime = ?,
                    status = 'new',
                    last_error = NULL,
                    optimized_at = NULL,
                    uploaded_at = NULL,
                    tv_content_id = NULL,
                    tv_deleted_at = NULL
                WHERE source_path = ?
                """,
                (mtime_ns, size, exif_datetime, sp),
            )

            # Reset upload history for this image; its bytes have changed.
            conn.execute("DELETE FROM uploads WHERE image_id = ?", (image_id,))

    def iter_pending_optimize(self, *, limit: int = 50) -> Iterable[ImageRow]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM images
                WHERE status IN ('new', 'failed_optimize')
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        for r in rows:
            yield ImageRow(**dict(r))

    def mark_optimized(
        self,
        *,
        image_id: int,
        sha256: str,
        optimized_path: Path,
        exif_datetime: str | None,
        optimizer_version: int,
        optimized_resize_mode: str,
        optimized_background_color: str,
    ) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE images
                SET source_sha256 = ?,
                    optimized_path = ?,
                    exif_datetime = ?,
                    optimizer_version = ?,
                    optimized_resize_mode = ?,
                    optimized_background_color = ?,
                    status = 'optimized',
                    last_error = NULL,
                    optimized_at = ?
                WHERE id = ?
                """,
                (
                    sha256,
                    str(optimized_path),
                    exif_datetime,
                    optimizer_version,
                    optimized_resize_mode,
                    optimized_background_color,
                    now,
                    image_id,
                ),
            )

    def mark_optimize_failed(self, *, image_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE images SET status = 'failed_optimize', last_error = ? WHERE id = ?",
                (error, image_id),
            )

    def mark_source_missing(self, *, image_id: int, source_path: str) -> None:
        """Mark a row missing when the source file no longer exists on disk.

        We clear optimized_path so it won't be selected for uploads/cycling.
        """

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE images
                SET status = 'missing',
                    last_error = ?,
                    optimized_path = NULL,
                    optimizer_version = NULL,
                    optimized_resize_mode = NULL,
                    optimized_background_color = NULL,
                    optimized_at = NULL
                WHERE id = ?
                """,
                (f"Source file missing: {source_path}", image_id),
            )

    def get_next_to_upload(self) -> ImageRow | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM images
                WHERE status = 'optimized'
                  AND (uploaded_at IS NULL)
                  AND (tv_content_id IS NULL)
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return ImageRow(**dict(row))

    def get_last_uploaded_at(self) -> str | None:
        """Return the most recent uploaded_at ISO timestamp (UTC) from upload history."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(uploaded_at) AS last_uploaded_at
                FROM uploads
                WHERE uploaded_at IS NOT NULL
                """
            ).fetchone()
        if row is None:
            return None
        return row["last_uploaded_at"]

    def mark_uploaded(self, *, image_id: int, tv_content_id: str) -> None:
        """Record a successful upload.

        We store upload history in the uploads table (so we can cycle/re-upload) and
        also update the images table with the latest upload for convenience.
        """

        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads(image_id, tv_content_id, uploaded_at)
                VALUES (?, ?, ?)
                """,
                (image_id, tv_content_id, now),
            )

            conn.execute(
                """
                UPDATE images
                SET status = 'uploaded',
                    uploaded_at = ?,
                    tv_content_id = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (now, tv_content_id, image_id),
            )

    def mark_upload_failed(self, *, image_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE images SET status = 'failed_upload', last_error = ? WHERE id = ?",
                (error, image_id),
            )

    def iter_uploaded_not_deleted(self) -> Iterable[ImageRow]:
        """Backwards-compatible iterator over images with a non-deleted last upload."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM images
                WHERE status = 'uploaded'
                  AND tv_content_id IS NOT NULL
                  AND tv_deleted_at IS NULL
                ORDER BY uploaded_at ASC
                """
            ).fetchall()
        for r in rows:
            yield ImageRow(**dict(r))

    def iter_uploads_not_deleted(self) -> Iterable[UploadRow]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM uploads
                WHERE tv_deleted_at IS NULL
                ORDER BY uploaded_at ASC
                """
            ).fetchall()
        for r in rows:
            yield UploadRow(**dict(r))

    def mark_upload_tv_deleted(self, *, tv_content_id: str) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE uploads SET tv_deleted_at = ? WHERE tv_content_id = ?",
                (now, tv_content_id),
            )
            conn.execute(
                "UPDATE images SET tv_deleted_at = ? WHERE tv_content_id = ?",
                (now, tv_content_id),
            )

    def get_next_new_image_to_upload(self) -> ImageRow | None:
        """Return the oldest optimized image that has never been uploaded."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT i.*
                FROM images AS i
                WHERE i.optimized_path IS NOT NULL
                  AND i.status IN ('optimized', 'uploaded', 'failed_upload')
                  AND NOT EXISTS (
                    SELECT 1 FROM uploads u WHERE u.image_id = i.id
                  )
                ORDER BY i.created_at ASC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return ImageRow(**dict(row))

    def list_cycle_images(self) -> list[ImageRow]:
        """All optimized images (unordered; caller decides order)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM images
                WHERE optimized_path IS NOT NULL
                  AND status IN ('optimized', 'uploaded', 'failed_upload')
                """
            ).fetchall()
        return [ImageRow(**dict(r)) for r in rows]

    def list_never_uploaded_images(self) -> list[ImageRow]:
        """Optimized images that have never been uploaded (unordered)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT i.*
                FROM images AS i
                WHERE i.optimized_path IS NOT NULL
                  AND i.status IN ('optimized', 'uploaded', 'failed_upload')
                  AND NOT EXISTS (
                    SELECT 1 FROM uploads u WHERE u.image_id = i.id
                  )
                """
            ).fetchall()
        return [ImageRow(**dict(r)) for r in rows]

    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return row["value"]

    def set_state(self, key: str, value: str | None) -> None:
        now = _utc_now_iso()
        with self._connect() as conn:
            if value is None:
                conn.execute("DELETE FROM state WHERE key = ?", (key,))
                return

            conn.execute(
                """
                INSERT INTO state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
