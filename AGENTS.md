# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

This repository contains:
- a Python CLI (`frame-upload`) that uploads images to Samsung The Frame TV via the `samsungtvws` library
- an optional headless app (`frame-uploader-app`) for folder watch + optimize + scheduled upload/select

User configuration is persisted to `~/.config/frame-uploader/` (including the TV host/IP in `config.json`).

## Development Commands

### Setup
```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

### Code Quality
```bash
# Run linter
ruff check .

# Run linter with auto-fix
ruff check --fix .
```

### Testing the CLI
```bash
# Configure TV connection
frame-upload configure --host <IP>

# Check connection + API
frame-upload status
frame-upload api-version

# Fetch a thumbnail for the currently displayed artwork
frame-upload thumbnail

# Check what's on the TV
frame-upload list
frame-upload current
```

### Running the headless app (single tick)
```bash
frame-uploader-app --once -v
```

## Architecture

The codebase is organized around a CLI and a small headless app:

**CLI layer** (`src/frame_uploader/cli.py`)
- Entry point via `frame-upload` command (defined in `pyproject.toml`)
- Uses `argparse` with subcommands: configure, configure-smartthings, upload, upload-alt, upload-async, list, current, thumbnail, api-version, select, delete, status
- Routes commands to uploader functions after loading config

**Upload/business logic** (`src/frame_uploader/uploader*.py`)
- Wraps `samsungtvws` with domain-specific functions
- Handles file type detection (JPEG/PNG), batch uploads, retries, and delays
- Upload variants live in separate modules (alt/async/smartthings)

**Configuration** (`src/frame_uploader/config.py`)
- `TVConfig` dataclass persisted as JSON at `~/.config/frame-uploader/config.json`
- TV authentication token auto-saved at `~/.config/frame-uploader/tv-token.txt`
- SmartThings fallback config (optional) at `~/.config/frame-uploader/smartthings.json`
- Default matte style is `modern_warm`

**Headless app** (`src/frame_uploader/app_*.py`, `src/frame_uploader/optimizer.py`)
- Folder scanning, optional optimization/resizing, and scheduled upload/select
- Persists a small SQLite DB (path configured via env var `DB_PATH` in Docker usage)

### Key Design Decisions

- Connection settings (including TV IP/host) live in `~/.config/frame-uploader/config.json`
- Upload delay (default 2.0s) between images prevents TV overload during bulk uploads
- Matte styles follow pattern `{type}_{color}` (e.g., `modern_apricot`, `flexible_warm`)
- Content IDs returned from uploads are in format `MY-F####` for user uploads, `SAM-F####` for built-in art

## TV Connection Notes

- First connection requires accepting prompt on TV screen
- TV must have a static IP address
- Connection uses WebSocket on port 8002 (default)
- Token file persistence eliminates need to re-authorize on subsequent connections

## Git
Commits should never be co-authored by an agent i.e. warp