#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPTIMIZED_DIR="${ROOT_DIR}/data/optimized"
DEFAULT_DB_PATH="${ROOT_DIR}/data/db/frame_uploader.sqlite3"

usage() {
  cat <<'EOF'
Usage:
  ./scripts/reset-optimized.sh --yes [--db PATH]

What it does:
  - deletes files in ./data/optimized/
  - resets optimization fields in the SQLite DB (so files will be re-optimized)

What it keeps:
  - ./data/incoming/ (source images)
  - ./data/config/   (TV config/token)
  - upload history in DB (uploads table) is kept
EOF
}

YES=false
DB_PATH="$DEFAULT_DB_PATH"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)
      YES=true
      shift
      ;;
    --db)
      DB_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ "$YES" != "true" ]]; then
  usage
  echo
  echo "Refusing to run without --yes" >&2
  exit 2
fi

mkdir -p "$OPTIMIZED_DIR"

# Clear optimized folder contents (keep directory).
find "$OPTIMIZED_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

# Clear DB pointers so the app will regenerate optimized images.
if [[ -f "$DB_PATH" ]]; then
  python - <<PY
import sqlite3

db_path = r"$DB_PATH"

conn = sqlite3.connect(db_path)
try:
    cur = conn.cursor()
    # Ensure table exists
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='images'")
    if cur.fetchone() is None:
        raise SystemExit(0)

    # Ensure expected columns exist (older DBs may not have them yet).
    cols = {row[1] for row in cur.execute("PRAGMA table_info(images)").fetchall()}

    if "optimizer_version" not in cols:
        cur.execute("ALTER TABLE images ADD COLUMN optimizer_version INTEGER")
    if "optimized_resize_mode" not in cols:
        cur.execute("ALTER TABLE images ADD COLUMN optimized_resize_mode TEXT")
    if "optimized_background_color" not in cols:
        cur.execute("ALTER TABLE images ADD COLUMN optimized_background_color TEXT")

    # Reset optimization-related fields; keep source paths + upload history.
    cur.execute(
        """
        UPDATE images
        SET optimized_path = NULL,
            optimizer_version = NULL,
            optimized_resize_mode = NULL,
            optimized_background_color = NULL,
            status = 'new',
            last_error = NULL,
            optimized_at = NULL
        """
    )

    conn.commit()
finally:
    conn.close()
PY
fi

echo "Reset complete: cleared optimized outputs and reset DB optimization fields."