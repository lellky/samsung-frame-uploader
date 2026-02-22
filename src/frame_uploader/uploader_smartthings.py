"""SmartThings API-based upload implementation.

Since the WebSocket/D2D upload returns error -1, this uses the SmartThings
cloud API as an alternative. Requires a SmartThings Personal Access Token.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def _file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "JPEG"
    if ext == ".png":
        return "PNG"
    raise ValueError(f"Unsupported file type: {ext}")


def upload_via_smartthings(
    token: str,
    device_id: str,
    image_path: Path,
    matte: str | None = None,
) -> str:
    """
    Upload image via SmartThings API.

    Args:
        token: SmartThings Personal Access Token
        device_id: SmartThings device ID for the Frame TV
        image_path: Path to the image file
        matte: Matte style (optional)

    Returns:
        Content ID of uploaded image

    Note:
        To get your token and device_id:
        1. Get token: https://account.smartthings.com/tokens
        2. Find device_id: Use `list_devices()` function below
    """
    api_base = "https://api.smartthings.com/v1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Read and encode image
    data = image_path.read_bytes()
    encoded = base64.b64encode(data).decode('utf-8')

    # Determine file type
    file_type = _file_type(image_path).lower()
    if file_type == "jpeg":
        file_type = "jpg"

    # Upload via SmartThings
    payload = {
        "commands": [
            {
                "component": "main",
                "capability": "samsungvd.artMode",
                "command": "uploadArtwork",
                "arguments": [
                    encoded,
                    file_type,
                    matte or "shadowbox_polar",
                ],
            }
        ]
    }

    url = f"{api_base}/devices/{device_id}/commands"

    try:
        logger.info(f"Uploading {image_path.name} via SmartThings API...")
        response = requests.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        result = response.json()
        logger.debug(f"SmartThings response: {result}")

        # The API doesn't return content_id immediately, need to query art list
        # after a delay
        import time
        time.sleep(3)

        # Get updated art list to find new content_id
        art_url = (
            f"{api_base}/devices/{device_id}/components/main/capabilities/"
            "samsungvd.artMode/status"
        )
        art_response = requests.get(art_url, headers=headers)
        art_response.raise_for_status()

        art_data = art_response.json()
        logger.debug(f"Art status: {art_data}")

        # Extract content ID from response (this may need adjustment based on actual response)
        if "artworkList" in art_data:
            artworks = art_data["artworkList"]["value"]
            if artworks:
                latest = artworks[0]
                return latest.get("contentId", "UNKNOWN")

        logger.warning("Could not extract content_id from SmartThings response")
        return "UPLOADED_VIA_SMARTTHINGS"

    except requests.RequestException as e:
        logger.error(f"SmartThings API error: {e}")
        raise RuntimeError(f"SmartThings upload failed: {e}")


def list_devices(token: str) -> list[dict]:
    """
    List all SmartThings devices to find your Frame TV device_id.

    Args:
        token: SmartThings Personal Access Token

    Returns:
        List of device dictionaries
    """
    api_base = "https://api.smartthings.com/v1"
    headers = {
        "Authorization": f"Bearer {token}",
    }

    try:
        response = requests.get(f"{api_base}/devices", headers=headers)
        response.raise_for_status()

        devices = response.json().get("items", [])

        # Filter for Frame TVs
        frame_devices = [
            d
            for d in devices
            if "frame" in d.get("label", "").lower()
            or "frame" in d.get("name", "").lower()
        ]

        if frame_devices:
            logger.info(f"Found {len(frame_devices)} Frame TV(s):")
            for device in frame_devices:
                logger.info(f"  - {device.get('label')}: {device.get('deviceId')}")
            return frame_devices
        else:
            logger.warning("No Frame TVs found. All devices:")
            for device in devices:
                logger.info(f"  - {device.get('label')}: {device.get('deviceId')}")
            return devices

    except requests.RequestException as e:
        logger.error(f"Failed to list devices: {e}")
        return []


def configure_smartthings(config_dir: Path, token: str, device_id: str):
    """Save SmartThings credentials to config file."""
    import json

    config_file = config_dir / "smartthings.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    config_data = {
        "token": token,
        "device_id": device_id,
    }

    config_file.write_text(json.dumps(config_data, indent=2))
    logger.info(f"SmartThings config saved to {config_file}")


def load_smartthings_config(config_dir: Path) -> tuple[str, str]:
    """Load SmartThings credentials from config file."""
    import json

    config_file = config_dir / "smartthings.json"
    if not config_file.exists():
        raise FileNotFoundError(
            "SmartThings not configured. Run: frame-upload configure-smartthings"
        )

    config_data = json.loads(config_file.read_text())
    return config_data["token"], config_data["device_id"]
