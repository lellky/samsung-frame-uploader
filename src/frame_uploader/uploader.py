"""Core logic for uploading images to Samsung The Frame."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from samsungtvws import SamsungTVWS

from frame_uploader.config import DEFAULT_CONFIG_PATH, TVConfig

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


class ArtModeRequiredError(RuntimeError):
    """Raised when an operation requires the TV to be *currently* in Art Mode."""


def _normalize_on(value) -> bool | None:
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value != 0

    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"on", "true", "1", "yes", "y", "enabled"}:
            return True
        if v in {"off", "false", "0", "no", "n", "disabled"}:
            return False

    return None


def is_in_art_mode(tv: SamsungTVWS) -> bool:
    """Best-effort check for whether the TV is *currently* in Art Mode.

    Important: If we cannot determine the state reliably, we return False.
    """

    try:
        art = tv.art()
    except Exception:  # noqa: BLE001
        return False

    try:
        if not art.supported():
            return False
    except Exception:  # noqa: BLE001
        return False

    # Try known helper methods exposed by some samsungtvws versions/forks.
    for name in (
        "get_artmode",
        "get_art_mode",
        "get_artmode_status",
        "get_art_mode_status",
        "artmode_status",
        "get_artmode_state",
        "get_art_mode_state",
    ):
        fn = getattr(art, name, None)
        if fn is None:
            continue

        try:
            result = fn()
        except Exception:  # noqa: BLE001
            continue

        b = _normalize_on(result)
        if b is not None:
            return b

        if isinstance(result, dict):
            for k in ("value", "status", "mode", "state", "artmode", "artMode", "art_mode"):
                if k in result:
                    b = _normalize_on(result[k])
                    if b is not None:
                        return b

    # Fallback: some TVs/libraries only expose get_current() when in Art Mode.
    try:
        cur = art.get_current()
    except Exception:  # noqa: BLE001
        return False

    if isinstance(cur, dict):
        for k in ("artmode", "artMode", "art_mode", "is_art_mode", "isArtMode"):
            if k in cur:
                b = _normalize_on(cur[k])
                if b is not None:
                    return b

        cid = cur.get("content_id") or cur.get("contentId") or cur.get("contentID")
        if isinstance(cid, str) and cid:
            return True

    return False


def ensure_in_art_mode(tv: SamsungTVWS, *, action: str) -> None:
    if not is_in_art_mode(tv):
        raise ArtModeRequiredError(
            f"TV is not currently in Art Mode (or the state could not be determined); "
            f"refusing to {action}. Switch the TV to Art Mode and try again."
        )


def _is_my_upload_id(content_id: str) -> bool:
    return content_id.startswith("MY_") or content_id.startswith("MY-")


def _file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "JPEG"
    if ext == ".png":
        return "PNG"
    raise ValueError(f"Unsupported file type: {ext}")


def connect(cfg: TVConfig) -> SamsungTVWS:
    """Create a connected SamsungTVWS instance."""
    token_path = Path(cfg.token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    return SamsungTVWS(host=cfg.host, port=cfg.port, token_file=str(token_path))


def check_art_mode(tv: SamsungTVWS) -> bool:
    """Return True if the TV supports art mode."""
    return tv.art().supported()


def list_art(tv: SamsungTVWS) -> list:
    """List all art currently on the TV."""
    return tv.art().available()


def get_current(tv: SamsungTVWS) -> dict:
    """Get info about the currently displayed art."""
    return tv.art().get_current()


def upload_image(tv: SamsungTVWS, path: Path, matte: str | None = None) -> str:
    """Upload a single image to the TV.

    For some older Frame TVs and/or samsungtvws builds, the WS/D2D upload path can
    fail with a generic error code (-1). In that case we:
    1) check whether the upload actually succeeded anyway
    2) if it did not, optionally fall back to SmartThings upload if configured

    Returns the content_id (best-effort).
    """

    ensure_in_art_mode(tv, action="upload images")

    import inspect

    from samsungtvws.exceptions import ResponseError

    data = path.read_bytes()
    ft = _file_type(path)

    kwargs: dict = {"file_type": ft}
    if matte:
        kwargs["matte"] = matte
        # portrait_matte is only supported in newer library versions (3.x+)
        upload_sig = inspect.signature(tv.art().upload)
        if "portrait_matte" in upload_sig.parameters:
            kwargs["portrait_matte"] = matte

    # Get list of existing images before upload
    before_ids = {
        img.get("content_id")
        for img in tv.art().available()
        if isinstance(img.get("content_id"), str) and _is_my_upload_id(img["content_id"])
    }

    try:
        response = tv.art().upload(data, **kwargs)
        logger.info("Uploaded %s: %s", path.name, response)
        return response
    except ResponseError as e:
        # Workaround for Frame TVs that return error -1 even on successful upload
        if "error number -1" not in str(e):
            raise

        logger.warning("Got error -1, checking if upload actually succeeded...")
        time.sleep(8)  # Give TV more time to process

        after_ids = {
            img.get("content_id")
            for img in tv.art().available()
            if isinstance(img.get("content_id"), str) and _is_my_upload_id(img["content_id"])
        }
        new_ids = after_ids - before_ids

        if new_ids:
            new_id = list(new_ids)[0]
            logger.info("Upload succeeded despite error -1! New image: %s", new_id)
            return new_id

        logger.error("Upload failed (error -1) and no new image appeared on the TV.")

        # Optional SmartThings fallback.
        try:
            from frame_uploader.uploader_smartthings import (
                load_smartthings_config,
                upload_via_smartthings,
            )

            config_dir = DEFAULT_CONFIG_PATH.parent
            token, device_id = load_smartthings_config(config_dir)
            logger.warning(
                "Falling back to SmartThings upload (configured at %s)", config_dir
            )
            return upload_via_smartthings(token, device_id, path, matte=matte)
        except FileNotFoundError:
            # Not configured; re-raise original upload error.
            raise e
        except Exception as st_err:
            logger.error("SmartThings fallback upload failed: %s", st_err)
            raise e from st_err


def upload_directory(
    tv: SamsungTVWS,
    directory: Path,
    matte: str | None = None,
    delay: float = 2.0,
) -> list[str]:
    """Upload all supported images in a directory. Returns list of responses."""
    files = sorted(
        f for f in directory.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not files:
        logger.warning("No supported images found in %s", directory)
        return []

    responses = []
    for f in files:
        logger.info("Uploading %s ...", f.name)
        resp = upload_image(tv, f, matte=matte)
        responses.append(resp)
        time.sleep(delay)
    return responses


def select_image(tv: SamsungTVWS, content_id: str) -> None:
    """Set a piece of art as the current display."""
    ensure_in_art_mode(tv, action="select images")
    tv.art().select_image(content_id)
    logger.info("Selected art: %s", content_id)


def delete_image(tv: SamsungTVWS, content_id: str) -> None:
    """Delete an uploaded piece of art."""
    tv.art().delete(content_id)
    logger.info("Deleted art: %s", content_id)
