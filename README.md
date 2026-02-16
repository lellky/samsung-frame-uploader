# Samsung Frame Uploader

A CLI tool to upload images to a Samsung The Frame TV using the [samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api) library.

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

```bash
# Upload a single image
frame-upload upload photo.jpg

# Upload all images in a directory
frame-upload upload ./images/

# Upload with a specific matte style
frame-upload upload photo.jpg --matte flexible_apricot

# List all art on the TV
frame-upload list

# Show currently displayed art
frame-upload current

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
