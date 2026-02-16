"""Core logic for uploading images to Samsung The Frame."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from samsungtvws import SamsungTVWS

from frame_uploader.config import TVConfig

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


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
    """Upload a single image to the TV. Returns the upload response."""
    data = path.read_bytes()
    ft = _file_type(path)
    kwargs: dict = {"file_type": ft}
    if matte:
        kwargs["matte"] = matte
        kwargs["portrait_matte"] = matte
    response = tv.art().upload(data, **kwargs)
    logger.info("Uploaded %s: %s", path.name, response)
    return response


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
    tv.art().select_image(content_id)
    logger.info("Selected art: %s", content_id)


def delete_image(tv: SamsungTVWS, content_id: str) -> None:
    """Delete an uploaded piece of art."""
    tv.art().delete(content_id)
    logger.info("Deleted art: %s", content_id)
