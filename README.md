# Samsung Frame Uploader

A CLI tool to upload images to a Samsung The Frame TV using the [samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api) library.

This repo includes:
- A **thumbnail downloader** (works on older Art API versions like 0.97)
- An **optional SmartThings fallback upload** path (requires a SmartThings Personal Access Token)
- A **headless scheduled uploader app** (`frame-uploader-app`) designed to run well in Docker

**Working models**: Most 2020+ Frame TVs (Art API 4.x+) should work out of the box. Some older models (e.g. Art API 0.97) may require a `samsungtvws` build that includes the Art API 0.97 upload fix.

## Quickstart (Docker Compose, recommended for a new host)

This is the easiest way to run the **headless scheduled uploader app** (`frame-uploader-app`) on a fresh machine without worrying about the host Python version.

Prereqs: Docker + Docker Compose v2 (you should have the `docker compose` command).

1) Clone + enter the repo
```bash
# HTTPS
git clone https://github.com/lellky/samsung-frame-uploader.git
cd samsung-frame-uploader
```

(If you prefer SSH: `git clone git@github.com:lellky/samsung-frame-uploader.git`.)

2) **Required (once): create the bind-mount directories**

Docker Compose bind-mounts `./data/...` into the container. Those directories must exist on the host before you start.

```bash
./scripts/init-data.sh
```

(If you get `permission denied`, run: `chmod +x ./scripts/init-data.sh` and try again.)

Optional: copy images into `./data/incoming/` on the host (or edit `docker-compose.yml` to change the bind mounts / `INPUT_DIR`).

3) Configure the TV connection (writes config/token under `./data/config/`)
```bash
docker compose run --rm frame-uploader-app frame-upload configure --host <TV_IP>
```

The first time you connect, you may need to accept the connection prompt on your TV.

4) Start the app
```bash
docker compose up -d --build
```

5) Verify
```bash
# One-off tick (verbose)
docker compose run --rm frame-uploader-app frame-uploader-app --once -v

# Check logs
docker compose logs -f frame-uploader-app
```

## Local install (Python)

If you only want the CLI locally (or you’re developing), you can install the project directly. Requires **Python 3.9+**.

```bash
pip install -e ".[dev]"
```

## Setup (configure TV)

Set your TV's static IP address, then configure.

- Local install:
  ```bash
  frame-upload configure --host <TV_IP>
  ```
- Docker Compose:
  ```bash
  docker compose run --rm frame-uploader-app frame-upload configure --host <TV_IP>
  ```

## Usage

Tip: if you’re running via Docker Compose, you can run the CLI inside the container like this:
```bash
docker compose run --rm frame-uploader-app frame-upload status
```

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
frame-upload configure --host <TV_IP> --port 8001  # For older TVs
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
- cycles through existing images in order, alternating with new images when both are available
- prunes images on the TV to enforce `KEEP_ON_TV` (see "TV cleanup behavior" below)

### Rotation and selection retries

`ORDER_MODE` controls image order; the default, `created_at`, uses when the app
first discovered each source file. `random` is a stable shuffled order rather than
an independent random choice on every tick.

New images get an early display without resetting progress through the existing
collection. When both groups are available, the app alternates one new image with
one image from the existing round. For example, a new image F arriving after A and
B can produce `A, B, F, C, D, ...` instead of restarting at A.

Each existing round has a saved, finite list of remaining image IDs. Later arrivals
join a subsequent round, so a continuous inflow cannot extend the current round
forever. The remaining list and ordinary rotation position persist across restarts.

Successful upload and successful selection are tracked separately. If selecting
an uploaded image fails, the app saves a pending selection and retries that same
TV content ID on the next reachable tick, before considering another image. It
does not upload another copy or advance the rotation while that selection is
pending. Retries bypass upload throttling, and after a successful retry the image
gets a full `UPLOAD_INTERVAL_SECONDS` before the next upload.

Cleanup preserves both the current artwork and artwork awaiting selection, which
can temporarily exceed `KEEP_ON_TV`. If the pending artwork has disappeared from
the TV, it is uploaded again; a removed or replaced source cancels its stale
pending selection. `DRY_RUN` leaves pending selections and rotation progress alone.
With `SELECT_AFTER_UPLOAD=false`, a successful upload completes the rotation step
without selecting artwork.

### HEIC / HEIF input

The scheduled app accepts JPEG, PNG, HEIC (`.heic`) and HEIF (`.heif`) files in
`incoming`, including uppercase extensions. HEIC/HEIF images are decoded with
`pillow-heif` and converted to the same 3840x2160 JPEG output as other images.
Original files are left unchanged. Orientation and EXIF capture dates are read
before optimization; capture dates can be used with `ORDER_MODE=exif_date`.
For files containing multiple images, only the primary still image is used.

Rebuild the Docker image after updating (`docker compose up -d --build`), or
reinstall the package for a local installation (`pip install -e .`). HEIC/HEIF
support is limited to the scheduled app; direct CLI uploads still accept JPEG
and PNG.

### Startup and upload timing (important)

`frame-uploader-app` runs a "tick" immediately on startup (there is no initial sleep).

However, the first tick may still **not upload** right away due to throttling and/or optimization:

- **Upload throttling across restarts**: `UPLOAD_INTERVAL_SECONDS` (default 300) is enforced from the
  later of the most recent successful upload and completed selection, recorded in SQLite. If you restart
  shortly after either operation, uploads wait until enough time has elapsed. Pending selection retries
  can still run immediately.

  Note: the "Upload throttled by UPLOAD_INTERVAL_SECONDS" message is logged at **DEBUG** level, so you
  generally need `frame-uploader-app -v` (or `--once -v`) to see it.

- **Optimization happens before uploading**: each tick scans and optimizes images before calling upload.
  If you have many new/large images, the tick can take a while before the first upload/select occurs.

- **TV asleep / energy saving mode**: if the TV is offline/unreachable on the network (common when it
  enters deep sleep / energy saving), the app will **skip upload and TV cleanup** for that tick (instead of
  crashing) and will try again on the next tick. You’ll typically see log lines like:
  - `Skipping upload: TV not reachable (HOST:PORT)`
  - `Skipping cleanup: TV not reachable (HOST:PORT)`

If you want the app to attempt an upload on every tick, set `UPLOAD_INTERVAL_SECONDS=0`.

By default, the `Dockerfile` installs dependencies from `pyproject.toml` (including `samsungtvws` from PyPI).

If you need to override the `samsungtvws` install (for example, to use a specific fork/branch), you can pass a build arg (see `Dockerfile`).

Note: the `lellky` fork mentioned in examples/config is intended as a **temporary workaround** for upstream issue **xchwarze/samsung-tv-ws-api#130** (https://github.com/xchwarze/samsung-tv-ws-api/issues/130). Once that issue is fixed in an upstream release, you should switch back to the normal PyPI dependency.

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
- `app_only` (default): deletes only content IDs previously uploaded by `frame-uploader-app` (tracked in its SQLite DB), leaving pre-existing uploads untouched. Before pruning, the app reconciles its history against the TV's current artwork list: uploads no longer present are marked as removed locally, without sending another delete command. Only remaining uploads count toward `KEEP_ON_TV`.
- `all_my`: treats *any* user-uploaded artwork on the TV as eligible for cleanup (content IDs starting with `MY_` or `MY-`), even if it existed before the app started.

Note: cleanup is best-effort and tries to avoid deleting the currently displayed artwork.
If the artwork list cannot be fetched or is malformed, cleanup leaves the history unchanged.
`DRY_RUN=true` logs planned cleanup and reconciliation without changing the TV or upload history.

You can run a single tick for testing:
```bash
# Local install
frame-uploader-app --once -v

# Docker Compose
docker compose run --rm frame-uploader-app frame-uploader-app --once -v
```

A sample `docker-compose.yml` is included (see **Quickstart** above).

**IMPORTANT (new host):** before you start the container, create the bind-mount directories on the host:
```bash
./scripts/init-data.sh
```

This is safe to re-run (it just `mkdir -p`s the required `./data/...` folders).

If you want to use Dropbox as the source of incoming files on a headless machine, see **[`docs/dropbox.md`](docs/dropbox.md)**.

## Documentation

- **[AGENTS.md](AGENTS.md)** - Development and testing guide
- **[`docs/dropbox.md`](docs/dropbox.md)** - Populate the incoming folder from Dropbox (rclone + systemd)

## Development

See [AGENTS.md](AGENTS.md) for architecture details and development commands.
