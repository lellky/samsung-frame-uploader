"""Command-line interface for Samsung Frame image uploader."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from frame_uploader.config import DEFAULT_CONFIG_PATH, TVConfig
from frame_uploader.thumbnails import fetch_thumbnail
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
from frame_uploader.uploader_alt import upload_image_chunked, upload_image_http_fallback
from frame_uploader.uploader_async import upload_image_async_sync_wrapper

logger = logging.getLogger(__name__)


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
    cfg_p.add_argument(
        "--matte",
        default="modern_warm",
        help="Default matte style (default: modern_warm)",
    )

    # --- configure-smartthings ---
    st_p = sub.add_parser(
        "configure-smartthings",
        help="Configure SmartThings fallback upload (token + device id)",
    )
    st_p.add_argument("--token", required=True, help="SmartThings Personal Access Token")
    st_p.add_argument("--device-id", required=True, help="SmartThings device ID for the TV")

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
    up_p.add_argument(
        "--retry",
        type=int,
        default=1,
        help="Number of retry attempts for failed uploads (default: 1)",
    )

    # --- upload-alt ---
    alt_p = sub.add_parser("upload-alt", help="Upload image(s) using alternative methods")
    alt_p.add_argument("path", type=Path, help="Image file or directory to upload")
    alt_p.add_argument("--matte", help="Matte style, e.g. 'modern_apricot', 'flexible_warm'")
    alt_p.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay between uploads in seconds (default: 2.0)",
    )
    alt_p.add_argument(
        "--method",
        choices=["chunked", "http", "both"],
        default="both",
        help="Upload method to try (default: both)",
    )

    # --- upload-async ---
    async_p = sub.add_parser("upload-async", help="Upload image(s) using async API")
    async_p.add_argument("path", type=Path, help="Image file or directory to upload")
    async_p.add_argument("--matte", help="Matte style, e.g. 'modern_apricot', 'flexible_warm'")
    async_p.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay between uploads in seconds (default: 2.0)",
    )

    # --- list ---
    sub.add_parser("list", help="List art on the TV")

    # --- current ---
    sub.add_parser("current", help="Show currently displayed art")

    # --- thumbnail ---
    thumb_p = sub.add_parser("thumbnail", help="Download a thumbnail JPEG for artwork")
    thumb_p.add_argument(
        "content_id",
        nargs="?",
        help="Content ID to fetch (default: current artwork)",
    )
    thumb_p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output file path (default: thumbnail_<content_id>.jpg)",
    )

    # --- select ---
    sel_p = sub.add_parser("select", help="Set a piece of art as current display")
    sel_p.add_argument("content_id", help="Content ID of the art to display")

    # --- delete ---
    del_p = sub.add_parser("delete", help="Delete uploaded art from the TV")
    del_p.add_argument("content_id", help="Content ID of the art to delete")

    # --- status ---
    sub.add_parser("status", help="Check if the TV supports art mode")

    # --- api-version ---
    sub.add_parser("api-version", help="Print the TV Art API version")

    return parser


def _configure(args: argparse.Namespace) -> None:
    config = TVConfig.load()
    config.host = args.host
    config.port = args.port
    config.matte = args.matte
    config.save()
    print(f"Configuration saved. TV host: {config.host}:{config.port}")


def _configure_smartthings(args: argparse.Namespace) -> None:
    from frame_uploader.uploader_smartthings import configure_smartthings

    config_dir = DEFAULT_CONFIG_PATH.parent
    configure_smartthings(config_dir, token=args.token, device_id=args.device_id)
    # Do not print token.
    print(f"SmartThings configuration saved to {config_dir / 'smartthings.json'}")


def _get_config() -> TVConfig:
    config = TVConfig.load()
    if not config.host:
        print(
            "Error: TV not configured. Run 'frame-upload configure --host <IP>' first.",
            file=sys.stderr,
        )
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

    if args.command == "configure-smartthings":
        _configure_smartthings(args)
        return

    from samsungtvws.exceptions import ResponseError

    config = _get_config()
    tv = connect(config)

    try:
        if args.command == "status":
            supported = check_art_mode(tv)
            print(f"Art mode supported: {supported}")

        elif args.command == "api-version":
            if not check_art_mode(tv):
                print("Error: TV does not support Art Mode.", file=sys.stderr)
                sys.exit(1)
            print(tv.art().get_api_version())

        elif args.command == "upload":
            matte = args.matte or config.matte
            path: Path = args.path
            max_retries = args.retry

            if path.is_file():
                if max_retries > 1:
                    resp = upload_image_chunked(
                        tv, path, matte=matte, max_retries=max_retries
                    )
                else:
                    resp = upload_image(tv, path, matte=matte)
                print(f"Uploaded: {resp}")
            elif path.is_dir():
                if max_retries > 1:
                    from frame_uploader.uploader_alt import SUPPORTED_EXTENSIONS

                    files = sorted(
                        f for f in path.iterdir()
                        if f.suffix.lower() in SUPPORTED_EXTENSIONS
                    )
                    if not files:
                        print(f"No supported images found in {path}", file=sys.stderr)
                        sys.exit(1)

                    responses = []
                    for f in files:
                        logging.info("Uploading %s ...", f.name)
                        resp = upload_image_chunked(
                            tv, f, matte=matte, max_retries=max_retries
                        )
                        responses.append(resp)
                        time.sleep(args.delay)
                    print(f"Uploaded {len(responses)} image(s)")
                else:
                    responses = upload_directory(tv, path, matte=matte, delay=args.delay)
                    print(f"Uploaded {len(responses)} image(s)")
            else:
                print(
                    f"Error: {path} is not a valid file or directory",
                    file=sys.stderr,
                )
                sys.exit(1)

        elif args.command == "upload-alt":
            matte = args.matte or config.matte
            path: Path = args.path
            method = args.method

            def try_upload(file_path: Path) -> str:
                """Try different upload methods based on user selection."""

                if method in ("chunked", "both"):
                    try:
                        return upload_image_chunked(
                            tv, file_path, matte=matte, max_retries=3
                        )
                    except Exception as e:
                        if method == "chunked":
                            raise
                        logging.warning(f"Chunked upload failed: {e}, trying HTTP...")

                if method in ("http", "both"):
                    try:
                        return upload_image_http_fallback(tv, file_path, matte=matte)
                    except Exception as e:
                        logging.error(f"HTTP upload failed: {e}")
                        raise

                raise RuntimeError("All upload methods failed")

            if path.is_file():
                resp = try_upload(path)
                print(f"Uploaded: {resp}")
            elif path.is_dir():
                from frame_uploader.uploader_alt import SUPPORTED_EXTENSIONS

                files = sorted(
                    f for f in path.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS
                )
                if not files:
                    print(f"No supported images found in {path}", file=sys.stderr)
                    sys.exit(1)

                responses = []
                for f in files:
                    logger.info("Uploading %s ...", f.name)
                    resp = try_upload(f)
                    responses.append(resp)
                    time.sleep(args.delay)
                print(f"Uploaded {len(responses)} image(s)")
            else:
                print(
                    f"Error: {path} is not a valid file or directory",
                    file=sys.stderr,
                )
                sys.exit(1)

        elif args.command == "list":
            art = list_art(tv)
            print(json.dumps(art, indent=2))

        elif args.command == "current":
            current = get_current(tv)
            print(json.dumps(current, indent=2))

        elif args.command == "thumbnail":
            if not check_art_mode(tv):
                print(
                    "Error: TV does not support Art Mode thumbnails.",
                    file=sys.stderr,
                )
                sys.exit(1)

            if args.content_id:
                content_id = args.content_id
            else:
                current = get_current(tv)
                content_id = current.get("content_id")
                if not content_id:
                    print(
                        "Error: Could not determine current content_id.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            suggested_name, data = fetch_thumbnail(tv.art(), content_id)

            out: Path
            if args.out is not None:
                out = args.out
            elif suggested_name:
                out = Path(suggested_name)
            else:
                out = Path(f"thumbnail_{content_id}.jpg")

            out.write_bytes(bytes(data))
            print(f"Wrote thumbnail for {content_id} -> {out} ({len(data)} bytes)")

        elif args.command == "select":
            select_image(tv, args.content_id)
            print(f"Selected: {args.content_id}")

        elif args.command == "delete":
            delete_image(tv, args.content_id)
            print(f"Deleted: {args.content_id}")

        elif args.command == "upload-async":
            matte = args.matte or config.matte
            path: Path = args.path

            if path.is_file():
                resp = upload_image_async_sync_wrapper(
                    config.host,
                    config.port,
                    config.token_file,
                    path,
                    matte=matte,
                )
                print(f"Uploaded: {resp}")
            elif path.is_dir():
                from frame_uploader.uploader_async import SUPPORTED_EXTENSIONS

                files = sorted(
                    f for f in path.iterdir()
                    if f.suffix.lower() in SUPPORTED_EXTENSIONS
                )
                if not files:
                    print(f"No supported images found in {path}", file=sys.stderr)
                    sys.exit(1)

                responses = []
                for f in files:
                    logging.info("Async uploading %s ...", f.name)
                    resp = upload_image_async_sync_wrapper(
                        config.host,
                        config.port,
                        config.token_file,
                        f,
                        matte=matte,
                    )
                    responses.append(resp)
                    time.sleep(args.delay)
                print(f"Uploaded {len(responses)} image(s)")
            else:
                print(
                    f"Error: {path} is not a valid file or directory",
                    file=sys.stderr,
                )
                sys.exit(1)

    except ResponseError as e:
        # Avoid a full stack trace for common TV/library issues.
        msg = str(e)
        print(f"Error: {msg}", file=sys.stderr)
        if "error number -1" in msg:
            print(
                "Hint: if WS upload is unreliable on your TV, configure SmartThings fallback: "
                "frame-upload configure-smartthings --token <PAT> --device-id <DEVICE_ID>",
                file=sys.stderr,
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
