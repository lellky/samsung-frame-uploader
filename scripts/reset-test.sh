#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INCOMING_DIR="${ROOT_DIR}/data/incoming"
OPTIMIZED_DIR="${ROOT_DIR}/data/optimized"
DB_DIR="${ROOT_DIR}/data/db"

usage() {
  cat <<'EOF'
Usage: ./scripts/reset-test.sh --yes

Deletes:
  - ./data/incoming/*
  - ./data/optimized/*
  - ./data/db/*   (including sqlite db + -wal/-shm)

Keeps:
  - ./data/config (TV config/token)
EOF
}

if [[ "${1:-}" != "--yes" ]]; then
  usage
  echo
  echo "Refusing to run without --yes"
  exit 2
fi

mkdir -p "$INCOMING_DIR" "$OPTIMIZED_DIR" "$DB_DIR"

# Remove contents but keep directories.
find "$INCOMING_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
find "$OPTIMIZED_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
find "$DB_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

echo "Reset complete: cleared incoming/, optimized/, db/ under ${ROOT_DIR}/data"