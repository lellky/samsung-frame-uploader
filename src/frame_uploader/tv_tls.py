"""Explicit SHA-256 certificate trust for Samsung's local TV transports.

SDK certificates need not name the TV's LAN address. An operator-provided pin
replaces CA/hostname trust, while certificate validity dates are still checked.
No peer is trusted automatically and no process-wide warning filters are used.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import socket
import ssl
from datetime import datetime, timezone

import requests
import websocket
from cryptography import x509
from requests.adapters import HTTPAdapter
from samsungtvws import SamsungTVArt, SamsungTVWS, exceptions, helper
from samsungtvws.event import (
    IGNORE_EVENTS_AT_STARTUP,
    MS_CHANNEL_CONNECT_EVENT,
    MS_CHANNEL_UNAUTHORIZED,
)
from samsungtvws.rest import SamsungTVRest
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool


class TVCertificateError(RuntimeError):
    """The TV cannot be reached using the configured certificate trust."""


def normalize_fingerprint(value: str) -> str:
    fingerprint = value.strip().replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Expected a SHA-256 certificate fingerprint (64 hexadecimal digits)")
    return fingerprint


def _check_certificate(certificate: bytes, fingerprint: str) -> None:
    actual = hashlib.sha256(certificate).hexdigest()
    if not hmac.compare_digest(actual, fingerprint):
        raise TVCertificateError("TV certificate fingerprint mismatch; connection refused")
    cert = x509.load_der_x509_certificate(certificate)
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
        raise TVCertificateError(
            "TV certificate is outside its validity period; connection refused"
        )


def open_pinned_socket(
    host: str, port: int, fingerprint: str, *, timeout: float | None = 10
) -> ssl.SSLSocket:
    """Verify the exact peer certificate before returning a usable TLS socket."""
    fingerprint = normalize_fingerprint(fingerprint)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Identity is checked against the explicit pin, rather than a public CA or
    # the LAN address (which the SDK certificate does not necessarily contain).
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        secured = context.wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise
    try:
        certificate = secured.getpeercert(binary_form=True)
        if not certificate:
            raise TVCertificateError("TV did not present a certificate; connection refused")
        _check_certificate(certificate, fingerprint)
    except BaseException:
        secured.close()
        raise
    return secured


class _PinnedHTTPSConnection(HTTPSConnection):
    def connect(self):
        if not self.assert_fingerprint:
            raise TVCertificateError("TV HTTPS connection is missing its required certificate pin")
        try:
            super().connect()
            _check_certificate(self.sock.getpeercert(binary_form=True), self.assert_fingerprint)
        except BaseException:
            self.close()
            raise


class _PinnedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _PinnedHTTPSConnection


class _PinnedHTTPSAdapter(HTTPAdapter):
    """Let urllib3 verify the fingerprint on every HTTPS connection, before HTTP."""

    def __init__(self, fingerprint: str):
        self.fingerprint = normalize_fingerprint(fingerprint)
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["assert_fingerprint"] = self.fingerprint
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            **self.poolmanager.pool_classes_by_scheme,
            "https": _PinnedHTTPSPool,
        }

    def send(self, request, **kwargs):
        # CA validation is replaced by urllib3's assert_fingerprint, not disabled
        # without an identity check. A verified pin also prevents urllib3's warning.
        kwargs["verify"] = False
        try:
            return super().send(request, **kwargs)
        except requests.exceptions.SSLError as error:
            raise TVCertificateError(
                "TV HTTPS certificate verification failed; connection refused"
            ) from error


class _PinnedRest(SamsungTVRest):
    def __init__(self, host: str, port: int, timeout: float | None, fingerprint: str):
        super().__init__(host, port=port, timeout=timeout)
        self.fingerprint = fingerprint

    def _is_ssl_connection(self) -> bool:
        return True

    def rest_ime_input(self, text: str, token: str | None = None) -> bool:
        # This optional upstream method bypasses _rest_request and embeds a token
        # in the URL. Do not let it use upstream's unverified requests path.
        raise TVCertificateError("REST keyboard input is unsupported in certificate-pinned mode")

    def _rest_request(self, target: str, method: str = "GET") -> dict:
        with requests.Session() as session:
            session.trust_env = False
            session.mount("https://", _PinnedHTTPSAdapter(self.fingerprint))
            with session.request(
                method,
                self._format_rest_url(target),
                timeout=self.timeout,
                allow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise TVCertificateError(
                        "TV HTTPS redirects are refused in certificate-pinned mode"
                    )
                response.raise_for_status()
                return helper.process_api_response(response.text)


class _PinnedChannel:
    """Open the library's channel over a socket authenticated before its handshake."""

    tls_fingerprint: str

    def _is_ssl_connection(self) -> bool:
        return True

    def open(self):
        if self.connection:
            return self.connection
        secured = open_pinned_socket(
            self.host, self.port, self.tls_fingerprint, timeout=self.timeout
        )
        try:
            channel = websocket.create_connection(
                self._format_websocket_url(self.endpoint),
                timeout=self.timeout,
                socket=secured,
                connection="Connection: Upgrade",
                redirect_limit=0,
            )
        except BaseException:
            secured.close()
            raise
        try:
            if channel.getstatus() != 101:
                raise TVCertificateError(
                    "TV WebSocket redirects are refused in certificate-pinned mode"
                )
            while True:
                response = helper.process_api_response(channel.recv())
                event = response.get("event", "*")
                self._websocket_event(event, response)
                if event not in IGNORE_EVENTS_AT_STARTUP:
                    break
            if event == MS_CHANNEL_UNAUTHORIZED:
                raise exceptions.UnauthorizedError(response)
            if event != MS_CHANNEL_CONNECT_EVENT:
                raise exceptions.ConnectionFailure(response)
            self._check_for_token(response)
            self.connection = channel
            # The Art subclass still consumes its ready event; the base connection
            # reuses the already authenticated channel without opening another socket.
            return super().open()
        except BaseException:
            self.connection = None
            channel.close()
            raise

    def _get_rest_api(self):
        if self._rest_api is None:
            self._rest_api = _PinnedRest(self.host, self.port, self.timeout, self.tls_fingerprint)
        return self._rest_api


class PinnedTVArt(_PinnedChannel, SamsungTVArt):
    def __init__(self, *args, tls_fingerprint: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.tls_fingerprint = normalize_fingerprint(tls_fingerprint)

    def _open_d2d_socket(self, conn_info: dict):
        if not conn_info.get("secured", False):
            raise TVCertificateError(
                "Unencrypted TV image transfers are refused in certificate-pinned mode"
            )
        return open_pinned_socket(
            conn_info["ip"],
            int(conn_info["port"]),
            self.tls_fingerprint,
            timeout=self.timeout,
        )


class PinnedTV(_PinnedChannel, SamsungTVWS):
    def __init__(self, *args, tls_fingerprint: str, **kwargs):
        super().__init__(*args, **kwargs)
        if self.port != 8002:
            raise ValueError("TV certificate verification requires TLS port 8002")
        if not hasattr(SamsungTVArt, "_open_d2d_socket"):
            raise RuntimeError("Certificate verification requires samsungtvws >= 3.0.6")
        self.tls_fingerprint = normalize_fingerprint(tls_fingerprint)

    def art(self):
        return PinnedTVArt(
            self.host,
            token=self.token,
            token_file=self.token_file,
            port=self.port,
            timeout=self.timeout,
            key_press_delay=self.key_press_delay,
            name=self.name,
            tls_fingerprint=self.tls_fingerprint,
        )
