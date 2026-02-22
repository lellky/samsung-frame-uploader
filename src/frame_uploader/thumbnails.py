"""Thumbnail fetching utilities for Samsung The Frame Art Mode.

The upstream `samsungtvws` package has multiple forks and versions. Some variants
(e.g. those based on NickWaterton's fork) historically assumed websocket frames
are always JSON-decodable UTF-8.

On older Frame TVs (Art API 0.97), thumbnail responses can be returned as a
single websocket *bytes* frame containing JSON followed by a raw JPEG tail.

This module provides a small compatibility layer so we can download thumbnails
reliably across these variants.
"""

from __future__ import annotations

import json
import random
import socket
from typing import Any

import websocket
from samsungtvws import helper


def _split_json_and_tail(buf: bytes) -> tuple[bytes, bytes]:
    """Split a bytes frame into (json_bytes, tail_bytes).

    The TV sometimes returns: b"{...json...}\n<binary>".
    We find the first JSON object and then match braces until it closes.
    """

    start = buf.find(b"{")
    if start < 0:
        raise ValueError("JSON start not found")

    depth = 0
    end: int | None = None
    for i in range(start, len(buf)):
        b = buf[i]
        if b == 0x7B:  # {
            depth += 1
        elif b == 0x7D:  # }
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end is None:
        raise ValueError("JSON end not found")

    json_bytes = buf[start:end]
    tail = buf[end:]
    if tail.startswith(b"\n"):
        tail = tail[1:]
    return json_bytes, tail


def _process_api_response_with_binary_tail(response: str | bytes) -> dict[str, Any]:
    """Replacement for samsungtvws.helper.process_api_response with tail support."""

    if isinstance(response, str):
        return json.loads(response)

    try:
        return json.loads(response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        json_bytes, tail = _split_json_and_tail(response)
        frame = json.loads(json_bytes.decode("utf-8"))
        if tail:
            frame["binary"] = tail
            frame["binary_len"] = len(tail)
        return frame


def ensure_thumbnail_compat() -> None:
    """Monkeypatch samsungtvws to handle Art API 0.97 inline-thumbnail frames.

    Some TVs return thumbnail JPEG bytes inline in a websocket *bytes* frame.
    Certain samsungtvws builds decode websocket frames with `json.loads(...)`
    directly, which fails for non-UTF-8 bytes.

    We patch two things when necessary:
    - `samsungtvws.helper.process_api_response` to split JSON + binary tail
    - `samsungtvws.art.SamsungTVArt.get_websocket_message` to propagate the
      parsed binary tail into the returned inner message dict
    """

    # 1) Patch helper.process_api_response if needed.
    current = getattr(helper, "process_api_response", None)
    if current is not _process_api_response_with_binary_tail:
        try:
            if current is not None:
                current(b"{}\n\xff\xd8\xff\xd9")  # type: ignore[misc]
            else:
                raise TypeError
        except Exception:
            helper.process_api_response = _process_api_response_with_binary_tail  # type: ignore[assignment]

    # 2) Patch SamsungTVArt.get_websocket_message to keep binary tails.
    try:
        from samsungtvws import art as art_mod
        from samsungtvws import exceptions
        from samsungtvws.event import D2D_SERVICE_MESSAGE_EVENT

        SamsungTVArt = getattr(art_mod, "SamsungTVArt", None)
        if SamsungTVArt is None:
            return

        original = SamsungTVArt.get_websocket_message
        if getattr(original, "_frame_uploader_binary_tail_patch", False):
            return

        def _get_websocket_message_with_binary(self):  # type: ignore[no-untyped-def]
            try:
                raw_data = self.connection.recv()
                response = helper.process_api_response(raw_data)
                event = response.get("event", "*")
                self._websocket_event(event, response)

                if event == D2D_SERVICE_MESSAGE_EVENT:
                    inner = json.loads(response["data"])
                    if "binary" in response:
                        inner["binary"] = response["binary"]
                        if "binary_len" in response:
                            inner["binary_len"] = response["binary_len"]
                    return inner

            except websocket.WebSocketTimeoutException as e:
                raise exceptions.TimeoutError(f"Websocket Time out: {e}") from e

            return {}

        _get_websocket_message_with_binary._frame_uploader_binary_tail_patch = True  # type: ignore[attr-defined]
        SamsungTVArt.get_websocket_message = _get_websocket_message_with_binary  # type: ignore[assignment]

    except Exception:
        # Best-effort; don't break the CLI if patching fails.
        return


def fetch_thumbnail(art: Any, content_id: str) -> tuple[str | None, bytearray]:
    """Fetch a thumbnail for a content id.

    Returns (suggested_filename, jpeg_bytes).

    Works for:
    - Art API 0.97: JPEG bytes returned inline in the websocket frame
    - Newer Art APIs: thumbnail bytes returned over a D2D socket

    Note: `art` is typically `tv.art()` (samsungtvws.art.SamsungTVArt).
    """

    ensure_thumbnail_compat()

    # This mirrors samsungtvws.art.SamsungTVArt.get_thumbnail() request shape.
    data = art._send_art_request(  # noqa: SLF001
        {
            "request": "get_thumbnail",
            "content_id": content_id,
            "conn_info": {
                "d2d_mode": "socket",
                "connection_id": random.randrange(4 * 1024 * 1024 * 1024),
                "id": art.get_uuid(),
            },
        }
    )

    if not data:
        raise RuntimeError("No response received from TV")

    inline = data.get("binary")
    if isinstance(inline, (bytes, bytearray)) and inline:
        return None, bytearray(inline)

    # D2D socket path.
    conn_info_raw = data.get("conn_info")
    if not isinstance(conn_info_raw, str) or not conn_info_raw:
        raise RuntimeError("Thumbnail response missing conn_info")

    conn_info = json.loads(conn_info_raw)

    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock_obj = (
        helper.get_ssl_context().wrap_socket(raw_sock)
        if conn_info.get("secured", False)
        else raw_sock
    )
    sock_obj.connect((conn_info["ip"], int(conn_info["port"])))

    header_len = int.from_bytes(sock_obj.recv(4), "big")
    header = json.loads(sock_obj.recv(header_len))

    thumbnail_len = int(header["fileLength"])
    filename = f"{header['fileID']}.{header['fileType']}"

    thumbnail = bytearray()
    while len(thumbnail) < thumbnail_len:
        packet = sock_obj.recv(thumbnail_len - len(thumbnail))
        thumbnail.extend(packet)

    sock_obj.close()
    return filename, thumbnail
