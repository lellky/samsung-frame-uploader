# Dropbox as an incoming source (rclone + systemd)
This project’s headless app (`frame-uploader-app`) reads images from an “incoming” folder (`INPUT_DIR`, default `/data/incoming` in Docker).
If you want to use Dropbox as the source of incoming files on a headless machine (e.g. an Intel NUC / Ubuntu server), a simple and robust pattern is:
1. Use `rclone` to periodically copy new images from a Dropbox folder to a local directory.
2. Point `frame-uploader-app` at that local directory (either directly, or via a Docker bind mount).

## Why this approach
- Keeps Dropbox integration out of the app (Dropbox becomes just a “delivery mechanism”).
- Works well on headless machines.
- Easy to troubleshoot (you can run the `rclone` command manually).

## Prerequisites
- A machine that can run `rclone` (the headless target).
- Any machine with a browser for the one-time OAuth authorization (can be your laptop).

## 1) Install rclone (Ubuntu)
```bash
sudo apt update
sudo apt install -y rclone
rclone version
```

## 2) Configure the Dropbox remote
On the headless machine:
```bash
rclone config
```
Create a new remote named `dropbox` and choose the **Dropbox** provider.

When prompted for browser/auto-config, pick **No** on the headless machine. `rclone` will ask for a token.

On a machine that has a browser:
```bash
rclone authorize "dropbox"
```
Copy the resulting token/config blob and paste it into the `rclone config` prompt on the headless machine.

Verify:
```bash
rclone lsd dropbox:
```

Tip: Dropbox paths shown in the Dropbox UI sometimes include “labels” that are not real folders. If you see `directory not found`, use `rclone lsd` to discover the actual remote paths.

## 3) Choose a local incoming directory
Pick a local directory that will be the “landing zone” for Dropbox files, for example:
```bash
mkdir -p "$HOME/frame-uploader/incoming"
```

## 4) Copy images from Dropbox on a schedule (systemd user timer)
This uses `systemd --user` so it runs as your user and can be enabled without running a root-owned daemon.

### 4.1 Create the service
Create `~/.config/systemd/user/frame-uploader-dropbox.service`:
```ini
[Unit]
Description=Copy images from Dropbox into incoming folder
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/rclone copy "dropbox:<DROPBOX_INCOMING_FOLDER>" "%h/frame-uploader/incoming" \
  --include "*.jpg" --include "*.jpeg" --include "*.png" --exclude "*" \
  --min-age 2m \
  --ignore-existing \
  --log-level INFO
```

Notes:
- Replace `<DROPBOX_INCOMING_FOLDER>` with your Dropbox folder path, e.g. `Photos/TheFrame`.
- Keep the Dropbox path quoted if it contains spaces.
- `--min-age 2m` avoids copying files that are still being uploaded.
- `--ignore-existing` prevents re-copying the same filenames (useful if you delete local files after processing).

### 4.2 Create the timer
Create `~/.config/systemd/user/frame-uploader-dropbox.timer`:
```ini
[Unit]
Description=Run Dropbox copy periodically

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s

[Install]
WantedBy=default.target
```

### 4.3 Enable “linger” (run without login) + start
```bash
sudo loginctl enable-linger "$USER"

systemctl --user daemon-reload
systemctl --user enable --now frame-uploader-dropbox.timer
```

### 4.4 Debugging
```bash
systemctl --user status frame-uploader-dropbox.timer
journalctl --user -u frame-uploader-dropbox.service --no-pager -n 200
```

## 5) Wire the local incoming directory into `frame-uploader-app`
You have two common options.

### Option A: Docker Compose bind-mount
If you run `frame-uploader-app` via Docker Compose, bind-mount your host incoming directory into the container as `/data/incoming`.

Example snippet:
```yaml
services:
  frame-uploader-app:
    # ...
    volumes:
      - /absolute/path/to/incoming:/data/incoming
      - ./data/optimized:/data/optimized
      - ./data/db:/data/db
      - ./data/config:/config
```

In this setup:
- `rclone` copies into `/absolute/path/to/incoming` on the host.
- The container sees those files at `/data/incoming`.

### Option B: Run the app directly (no Docker)
Run the app with `INPUT_DIR` set to your local incoming directory:
```bash
export INPUT_DIR="$HOME/frame-uploader/incoming"
frame-uploader-app --once -v
```

## Avoiding duplicates (optional)
If you want Dropbox to behave like a “queue” (process once), consider:
- switching `rclone copy` → `rclone move` (removes from the Dropbox incoming folder once copied), or
- keeping two Dropbox folders (Incoming + Archive) and moving processed files into Archive.
