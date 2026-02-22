"""Async upload implementation using aiohttp and websockets."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def _file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "JPEG"
    if ext == ".png":
        return "PNG"
    raise ValueError(f"Unsupported file type: {ext}")


async def upload_image_async(
    host: str,
    port: int,
    token_file: str,
    path: Path,
    matte: str | None = None,
) -> str:
    """Upload image using async API."""
    try:
        from samsungtvws.async_art import SamsungTVAsyncArt
    except ImportError:
        raise RuntimeError(
            "Async art support not available. "
            "Make sure you have Nick Waterton's fork installed: "
            "pip install git+https://github.com/NickWaterton/samsung-tv-ws-api.git"
        )
    
    data = path.read_bytes()
    ft = _file_type(path)
    
    logger.info(f"Async uploading {path.name}...")
    
    # Create async art interface
    art = SamsungTVAsyncArt(
        host=host,
        port=port,
        token_file=token_file,
    )
    
    try:
        # Start listening (establishes connection)
        await art.start_listening()
        
        # Check if supported
        supported = await art.supported()
        if not supported:
            raise RuntimeError("TV does not support art mode")
        
        logger.debug("Art mode supported")
        
        # Get current art list (before upload)
        before = await art.available()
        before_ids = {img.get('content_id') for img in before if img.get('content_id', '').startswith('MY_F')}
        logger.debug(f"Before IDs: {len(before_ids)} images")
        
        # Upload
        kwargs = {"file_type": ft}
        if matte:
            kwargs["matte"] = matte
            kwargs["portrait_matte"] = matte
        
        try:
            content_id = await art.upload(data, **kwargs)
            logger.info(f"Upload successful: {content_id}")
            return content_id
        except Exception as e:
            # Check if upload succeeded despite error
            if 'error number -1' in str(e) or '-1' in str(e):
                logger.warning(f"Got error but checking if upload succeeded: {e}")
                await asyncio.sleep(8)
                
                after = await art.available()
                after_ids = {img.get('content_id') for img in after if img.get('content_id', '').startswith('MY_F')}
                new_ids = after_ids - before_ids
                
                if new_ids:
                    new_id = list(new_ids)[0]
                    logger.info(f"Upload succeeded despite error! New image: {new_id}")
                    return new_id
                else:
                    logger.error("Upload truly failed - no new image found")
                    logger.error(f"Before: {len(before_ids)} images, After: {len(after_ids)} images")
                    raise
            else:
                raise
                
    finally:
        await art.close()


def upload_image_async_sync_wrapper(
    host: str,
    port: int,
    token_file: str,
    path: Path,
    matte: str | None = None,
) -> str:
    """Synchronous wrapper for async upload."""
    return asyncio.run(
        upload_image_async(host, port, token_file, path, matte)
    )
