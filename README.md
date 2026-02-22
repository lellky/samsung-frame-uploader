# Samsung Frame Uploader

A CLI tool to upload images to a Samsung The Frame TV using the [samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api) library.

This repo includes:
- A **thumbnail downloader** (works on older Art API versions like 0.97)
- An **optional SmartThings fallback upload** path (requires a SmartThings Personal Access Token)

**Working models**: Most 2020+ Frame TVs (Art API 4.x+) should work out of the box. Some older models (e.g. Art API 0.97) may require a `samsungtvws` build that includes the Art API 0.97 upload fix.

## Install

```bash
pip install -e ".[dev]"
```

## Setup

Set your TV's static IP address, then configure:

```bash
frame-upload configure --host 192.168.1.100
```

The first time you connect, you may need to accept the connection prompt on your TV.

## Usage

### Basic Upload

```bash
# Upload a single image
frame-upload upload photo.jpg

# Upload all images in a directory
frame-upload upload ./images/

# Upload with a specific matte style
frame-upload upload photo.jpg --matte flexible_apricot

# Upload with retry logic (for transient errors)
frame-upload upload photo.jpg --retry 3
```

### Alternative Upload Methods

If standard upload fails, try alternative methods:

```bash
# Try all available methods (chunked + HTTP fallback)
frame-upload upload-alt photo.jpg --method both

# Async upload (may work on some TVs)
frame-upload upload-async photo.jpg
```

### Other Commands

```bash
# List all art on the TV
frame-upload list

# Show currently displayed art
frame-upload current

# Download a thumbnail JPEG (defaults to current artwork)
frame-upload thumbnail
frame-upload thumbnail MY-F0123 --out /tmp/thumb.jpg

# Print the TV Art API version (e.g. 0.97, 4.0, ...)
frame-upload api-version

# Set a specific piece of art as the display
frame-upload select MY-F0001

# Delete uploaded art
frame-upload delete MY-F0001

# Check art mode support
frame-upload status
```

### Matte Styles

Matte format is `{type}_{color}`, for example `modern_apricot` or `flexible_warm`.

**Types:** none, modernthin, modern, modernwide, flexible, shadowbox, panoramic, triptych, mix, squares

**Colors:** black, neutral, antique, warm, polar, sand, seafoam, sage, burgandy, navy, apricot, byzantine, lavender, redorange, skyblue, turquoise

## Configuration

Config is stored at `~/.config/frame-uploader/config.json`. Auth token is persisted at `~/.config/frame-uploader/tv-token.txt`.

### SmartThings fallback (optional)

If your TV rejects uploads with `error number -1`, you can configure an *experimental* SmartThings fallback upload.

```bash
frame-upload configure-smartthings --token <SMARTTHINGS_PAT> --device-id <DEVICE_ID>
```

This writes `~/.config/frame-uploader/smartthings.json`.

### Port Configuration

- **Port 8001**: Older TVs (pre-2016) and some 2016-2018 models
- **Port 8002**: Newer TVs (2019+) with secure WebSocket (default)

```bash
frame-upload configure --host 192.168.1.100 --port 8001  # For older TVs
```

## Troubleshooting

If uploads fail:

1. Check your TV's Art API version:
   ```bash
   frame-upload api-version
   ```

2. Try different upload methods (see "Alternative Upload Methods" above)

3. Check if other commands work (list, current, status)

4. If you're on an older model (e.g. Art API 0.97) and uploads fail, ensure you're using a `samsungtvws` version that includes the Art API 0.97 upload fix.

### Known Issues / Compatibility Notes

- **Older Frame TVs (Art API 0.97)**: Thumbnails generally work. Upload support depends on having the Art API 0.97 upload fix in your installed `samsungtvws`.
- **2020+ Frame TVs (Art API 4.x+)**: Should work with standard upload methods

## Scheduled uploader app (folder watch + optimize + upload)

This repo also includes a headless app (`frame-uploader-app`) that:
- scans an input folder for images
- optimizes them for The Frame (3840x2160 JPEG)
- uploads *and selects* one image per interval (throttled by `UPLOAD_INTERVAL_SECONDS`)
- when there are no new images, it cycles through the existing image history and re-uploads in order
- prunes images on the TV to enforce `KEEP_ON_TV` (see "TV cleanup behavior" below)

By default, the `Dockerfile` installs dependencies from `pyproject.toml` (including `samsungtvws` from PyPI). If you need to override the `samsungtvws` install (for example, to use a specific fork/branch), you can pass a build arg (see `Dockerfile`).

Configuration is via environment variables (suitable for Docker):
- `INPUT_DIR`, `OPTIMIZED_DIR`, `DB_PATH`
- `INTERVAL_SECONDS` (default 300)
- `UPLOAD_INTERVAL_SECONDS` (default 300)
- `KEEP_ON_TV` (default 20)
- `JPEG_QUALITY` (default 85)
- `CLEANUP_MODE` (`app_only` default, or `all_my`)
- `SELECT_AFTER_UPLOAD` (default true)
- `RESIZE_MODE` (`pad` default, or `crop`)
- `BACKGROUND_COLOR` (default `black`)
- `MATTE` (default `none` for full-bleed, or set e.g. `modern_warm`)
- `ORDER_MODE` (`created_at` default, or `filename`, `exif_date`, `random`)
- `ORDER_SEED` (optional, used when `ORDER_MODE=random`)

### TV cleanup behavior

- `frame-upload` (the CLI) never deletes anything unless you run `frame-upload delete ...`.
- `frame-uploader-app` prunes on every tick after uploading.

`KEEP_ON_TV`:
- Maximum number of candidate images to keep on the TV.
- Set to a negative value (e.g. `-1`) to disable cleanup entirely.

`CLEANUP_MODE`:
- `app_only` (default): deletes only content IDs previously uploaded by `frame-uploader-app` (tracked in its SQLite DB), leaving pre-existing uploads untouched.
- `all_my`: treats *any* user-uploaded artwork on the TV as eligible for cleanup (content IDs starting with `MY_` or `MY-`), even if it existed before the app started.

Note: cleanup is best-effort and tries to avoid deleting the currently displayed artwork.

You can run a single tick for testing:
```bash
frame-uploader-app --once -v
```

A sample `docker-compose.yml` is included.

Before you start the container, create the bind-mount directories once:
```bash
./scripts/init-data.sh
```

If you want to use Dropbox as the source of incoming files on a headless machine, see **[`docs/dropbox.md`](docs/dropbox.md)**.

## Documentation

- **[AGENTS.md](AGENTS.md)** - Development and testing guide
- **[`docs/dropbox.md`](docs/dropbox.md)** - Populate the incoming folder from Dropbox (rclone + systemd)

## Development

See [AGENTS.md](AGENTS.md) for architecture details and development commands.
