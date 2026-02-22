#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p \
  "${ROOT_DIR}/data/incoming" \
  "${ROOT_DIR}/data/optimized" \
  "${ROOT_DIR}/data/db" \
  "${ROOT_DIR}/data/config"

echo "Initialized data directories under: ${ROOT_DIR}/data"
