"""Command-line interface for Samsung Frame image uploader."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from frame_uploader import config as cfg_mod
from frame_uploader.config import TVConfig
from frame_uploader.uploader import (
    check_art_mode,
    connect,
    delete_image,
    get_current,
    list_art,
    select_image,
    upload_directory,
    upload_image,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frame-upload",
        description="Upload and manage images on a Samsung The Frame TV.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    sub = parser.add_subparsers(dest="command")

    # --- configure ---
    cfg_p = sub.add_parser("configure", help="Set TV connection details")
    cfg_p.add_argument("--host", required=True, help="TV IP address")
    cfg_p.add_argument("--port", type=int, default=8002, help="TV port (default: 8002)")
    cfg_p.add_argument("--matte", default="modern_warm", help="Default matte style (default: modern_warm)")

    # --- upload ---
    up_p = sub.add_parser("upload", help="Upload image(s) to the TV")
    up_p.add_argument("path", type=Path, help="Image file or directory to upload")
    up_p.add_argument("--matte", help="Matte style, e.g. 'modern_apricot', 'flexible_warm'")
    up_p.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay between uploads in seconds (default: 2.0)",
    )

    # --- list ---
    sub.add_parser("list", help="List art on the TV")

    # --- current ---
    sub.add_parser("current", help="Show currently displayed art")

    # --- select ---
    sel_p = sub.add_parser("select", help="Set a piece of art as current display")
    sel_p.add_argument("content_id", help="Content ID of the art to display")

    # --- delete ---
    del_p = sub.add_parser("delete", help="Delete uploaded art from the TV")
    del_p.add_argument("content_id", help="Content ID of the art to delete")

    # --- status ---
    sub.add_parser("status", help="Check if the TV supports art mode")

    return parser


def _configure(args: argparse.Namespace) -> None:
    config = TVConfig.load()
    config.host = args.host
    config.port = args.port
    config.matte = args.matte
    config.save()
    print(f"Configuration saved. TV host: {config.host}:{config.port}")


def _get_config() -> TVConfig:
    config = TVConfig.load()
    if not config.host:
        print("Error: TV not configured. Run 'frame-upload configure --host <IP>' first.", file=sys.stderr)
        sys.exit(1)
    return config


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "configure":
        _configure(args)
        return

    config = _get_config()
    tv = connect(config)

    if args.command == "status":
        supported = check_art_mode(tv)
        print(f"Art mode supported: {supported}")

    elif args.command == "upload":
        matte = args.matte or config.matte
        path: Path = args.path
        if path.is_file():
            resp = upload_image(tv, path, matte=matte)
            print(f"Uploaded: {resp}")
        elif path.is_dir():
            responses = upload_directory(tv, path, matte=matte, delay=args.delay)
            print(f"Uploaded {len(responses)} image(s)")
        else:
            print(f"Error: {path} is not a valid file or directory", file=sys.stderr)
            sys.exit(1)

    elif args.command == "list":
        art = list_art(tv)
        print(json.dumps(art, indent=2))

    elif args.command == "current":
        current = get_current(tv)
        print(json.dumps(current, indent=2))

    elif args.command == "select":
        select_image(tv, args.content_id)
        print(f"Selected: {args.content_id}")

    elif args.command == "delete":
        delete_image(tv, args.content_id)
        print(f"Deleted: {args.content_id}")


if __name__ == "__main__":
    main()
