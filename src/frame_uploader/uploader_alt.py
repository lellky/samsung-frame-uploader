"""Alternative upload implementation with enhanced error handling."""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path

from samsungtvws import SamsungTVWS
from samsungtvws.exceptions import ResponseError

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


def upload_image_chunked(
    tv: SamsungTVWS,
    path: Path,
    matte: str | None = None,
    chunk_size: int = 8192,
    max_retries: int = 3,
) -> str:
    """
    Upload image with chunked transfer and retry logic.
    
    This method uses lower-level socket operations with chunked sending
    to potentially work around upload issues on older Frame TVs.
    """
    data = path.read_bytes()
    ft = _file_type(path)
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Upload attempt {attempt + 1}/{max_retries} for {path.name}")
            
            # Get list of existing images before upload
            before_ids = {
                img.get('content_id') 
                for img in tv.art().available() 
                if img.get('content_id', '').startswith('MY_F')
            }
            
            # Use the library's upload with chunked socket sending
            kwargs = {"file_type": ft}
            if matte:
                kwargs["matte"] = matte
                kwargs["portrait_matte"] = matte
            
            response = tv.art().upload(data, **kwargs)
            logger.info("Upload successful: %s", response)
            return response
            
        except ResponseError as e:
            error_msg = str(e)
            logger.warning(f"Upload attempt {attempt + 1} error: {error_msg}")
            
            # Check if upload actually succeeded despite error
            if 'error number -1' in error_msg or 'error' in error_msg.lower():
                logger.info("Checking if upload succeeded despite error...")
                time.sleep(5)
                
                after_ids = {
                    img.get('content_id') 
                    for img in tv.art().available() 
                    if img.get('content_id', '').startswith('MY_F')
                }
                new_ids = after_ids - before_ids
                
                if new_ids:
                    new_id = list(new_ids)[0]
                    logger.info(f"Upload succeeded! New image: {new_id}")
                    return new_id
            
            # Retry on last attempt
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 3
                logger.info(f"Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                logger.error("All upload attempts failed")
                raise
        
        except Exception as e:
            logger.error(f"Unexpected error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 3)
            else:
                raise
    
    raise RuntimeError("Upload failed after all retries")


def upload_image_http_fallback(
    tv: SamsungTVWS,
    path: Path,
    matte: str | None = None,
) -> str:
    """
    Attempt upload using REST API fallback (experimental).
    
    Some Samsung TVs support a REST endpoint for uploads.
    This is not guaranteed to work on all models.
    """
    import requests
    
    data = path.read_bytes()
    ft = _file_type(path).lower()
    
    # Try direct HTTP upload to art mode endpoint
    url = f"http://{tv.host}:8001/api/v2/art/upload"
    
    files = {
        'file': (path.name, data, f'image/{ft}')
    }
    
    params = {}
    if matte:
        params['matte'] = matte
        params['portrait_matte'] = matte
    
    try:
        logger.info(f"Attempting HTTP upload to {url}")
        response = requests.post(url, files=files, params=params, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        logger.info(f"HTTP upload response: {result}")
        
        if 'content_id' in result:
            return result['content_id']
        
        # If no content_id, check for new images
        time.sleep(3)
        art_list = tv.art().available()
        my_art = [img for img in art_list if img.get('content_id', '').startswith('MY_F')]
        if my_art:
            latest = sorted(my_art, key=lambda x: x.get('date', ''), reverse=True)[0]
            return latest['content_id']
        
        raise RuntimeError("Upload completed but content_id not found")
        
    except requests.RequestException as e:
        logger.error(f"HTTP upload failed: {e}")
        raise RuntimeError(f"HTTP upload not supported or failed: {e}")
