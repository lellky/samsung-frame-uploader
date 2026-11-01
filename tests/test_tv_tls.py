from __future__ import annotations

import hashlib
import ipaddress
import json
import ssl
import threading
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from urllib3.exceptions import InsecureRequestWarning
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from frame_uploader import cli, uploader
from frame_uploader.config import TVConfig
from frame_uploader.thumbnails import fetch_thumbnail
from frame_uploader.tv_tls import (
    PinnedTV,
    PinnedTVArt,
    TVCertificateError,
    _PinnedRest,
    normalize_fingerprint,
    open_pinned_socket,
)
from frame_uploader.uploader_alt import upload_image_http_fallback


@pytest.fixture
def certificate(tmp_path):
    def create(*, expired=False, future=False):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test SDK")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + timedelta(days=1) if future else now - timedelta(days=2))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=2))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_path = tmp_path / f"{cert.serial_number}.pem"
        key_path = tmp_path / f"{cert.serial_number}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        return SimpleNamespace(
            context=context,
            fingerprint=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
        )

    return create


@contextmanager
def https_server(cert, *, redirect=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, ssl.SSLEOFError):
                # Rejected clients close immediately after inspecting the cert.
                pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(302 if redirect else 200)
            if redirect:
                self.send_header("Location", "http://untrusted.invalid/")
            self.end_headers()
            self.wfile.write(b'{"device":{"FrameTVSupport":"true"}}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = cert.context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=server.server_port, requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def art_server(cert):
    requests = []
    uploads = []

    def handshake(connection, request):
        requests.append(request.path)

    def handler(connection):
        connection.send(json.dumps({"event": "ms.channel.connect", "data": {}}))
        connection.send(json.dumps({"event": "ms.channel.ready"}))
        try:
            for message in connection:
                if isinstance(message, bytes):
                    header_len = int.from_bytes(message[:2], "big")
                    outer = json.loads(message[2 : 2 + header_len])
                    data = json.loads(outer["params"]["data"])
                    uploads.append(message[2 + header_len :])
                    reply = {"event": "image_added", "content_id": "MY_test"}
                else:
                    outer = json.loads(message)
                    data = json.loads(outer["params"]["data"])
                    reply = {"event": "api_version", "version": "0.97"}
                reply["id"] = data["id"]
                if data["request"] == "get_thumbnail":
                    frame = json.dumps(
                        {"event": "d2d_service_message", "data": json.dumps(reply)}
                    ).encode()
                    connection.send(frame + b"\nthumbnail")
                else:
                    connection.send(
                        json.dumps({"event": "d2d_service_message", "data": json.dumps(reply)})
                    )
        except ConnectionClosed:
            pass

    server = serve(
        handler,
        "127.0.0.1",
        0,
        ssl=cert.context,
        process_request=handshake,
        open_timeout=2,
        close_timeout=1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            port=server.socket.getsockname()[1], requests=requests, uploads=uploads
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_https_pin_allows_sdk_certificate_without_matching_hostname(certificate):
    cert = certificate()
    with https_server(cert) as server, warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        # The certificate names 127.0.0.1 only, while the client uses localhost.
        rest = _PinnedRest("localhost", server.port, 2, cert.fingerprint)
        assert rest.rest_device_info()["device"]["FrameTVSupport"] == "true"
        assert server.requests == ["/api/v2/"]
        # The same untrusted certificate is still rejected by ordinary clients.
        with pytest.raises(requests.exceptions.SSLError):
            requests.get(f"https://localhost:{server.port}/", timeout=2)
        assert server.requests == ["/api/v2/"]
    assert not [w for w in recorded if issubclass(w.category, InsecureRequestWarning)]


def test_https_rejects_changed_certificate_before_sending_http(certificate):
    cert = certificate()
    other = certificate()
    with https_server(cert) as server:
        rest = _PinnedRest("localhost", server.port, 2, other.fingerprint)
        with pytest.raises(TVCertificateError):
            rest.rest_device_info()
        assert server.requests == []


@pytest.mark.parametrize("kind", ["expired", "future"])
def test_https_rejects_invalid_dates_before_sending_http(certificate, kind):
    cert = certificate(**{kind: True})
    with https_server(cert) as server:
        rest = _PinnedRest("localhost", server.port, 2, cert.fingerprint)
        with pytest.raises(TVCertificateError, match="validity period"):
            rest.rest_device_info()
        assert server.requests == []


def test_https_refuses_redirects(certificate):
    cert = certificate()
    with https_server(cert, redirect=True) as server:
        rest = _PinnedRest("localhost", server.port, 2, cert.fingerprint)
        with pytest.raises(TVCertificateError, match="redirects"):
            rest.rest_device_info()
        assert server.requests == ["/api/v2/"]


def test_art_upload_uses_authenticated_websocket(certificate):
    cert = certificate()
    with art_server(cert) as server:
        art = PinnedTVArt(
            "localhost",
            port=server.port,
            timeout=2,
            key_press_delay=0,
            token="synthetic-token",
            tls_fingerprint=cert.fingerprint,
        )
        try:
            assert art.upload(b"test image", file_type="JPEG", matte="none") == "MY_test"
            assert server.uploads == [b"test image"]
            assert len(server.requests) == 1
        finally:
            art.close()


def test_websocket_rejects_changed_certificate_before_sending_token(certificate):
    cert = certificate()
    other = certificate()
    with art_server(cert) as server:
        art = PinnedTVArt(
            "localhost",
            port=server.port,
            timeout=2,
            token="synthetic-token",
            tls_fingerprint=other.fingerprint,
        )
        with pytest.raises(TVCertificateError, match="mismatch"):
            art.open()
        assert art.connection is None
        assert server.requests == []


def test_websocket_refuses_redirects(certificate):
    cert = certificate()
    with https_server(cert, redirect=True) as server:
        art = PinnedTVArt("localhost", port=server.port, tls_fingerprint=cert.fingerprint)
        with pytest.raises(TVCertificateError, match="redirects"):
            art.open()
        assert len(server.requests) == 1


@pytest.mark.parametrize("kind", ["expired", "future"])
def test_websocket_rejects_invalid_certificate_dates(certificate, kind):
    cert = certificate(**{kind: True})
    with art_server(cert) as server:
        art = PinnedTVArt("localhost", port=server.port, tls_fingerprint=cert.fingerprint)
        with pytest.raises(TVCertificateError, match="validity period"):
            art.open()
        assert server.requests == []


def test_secure_d2d_socket_checks_pin(certificate):
    cert = certificate()
    other = certificate()
    with https_server(cert) as server:
        art = PinnedTVArt("localhost", tls_fingerprint=cert.fingerprint)
        info = {"secured": True, "ip": "localhost", "port": server.port}
        with art._open_d2d_socket(info) as secured:
            assert isinstance(secured, ssl.SSLSocket)
        art.tls_fingerprint = other.fingerprint
        with pytest.raises(TVCertificateError, match="mismatch"):
            art._open_d2d_socket(info)
        assert server.requests == []


def test_plaintext_d2d_is_rejected_before_connecting():
    art = PinnedTVArt("unused.invalid", tls_fingerprint="ab" * 32)
    with pytest.raises(TVCertificateError, match="Unencrypted"):
        art._open_d2d_socket({"secured": False})


def test_http_fallback_is_rejected_before_reading_or_sending_image(tmp_path):
    tv = PinnedTV("unused.invalid", port=8002, tls_fingerprint="ab" * 32)
    with pytest.raises(TVCertificateError, match="HTTP upload fallback"):
        upload_image_http_fallback(tv, tmp_path / "does-not-exist.jpg")


def test_rest_keyboard_input_cannot_bypass_verification():
    rest = _PinnedRest("unused.invalid", 8002, 2, "ab" * 32)
    with pytest.raises(TVCertificateError, match="unsupported"):
        rest.rest_ime_input("text", token="synthetic-token")


def test_connect_propagates_saved_pin_to_all_client_factories(tmp_path):
    cfg = TVConfig(
        host="unused.invalid", token_file=str(tmp_path / "token"), tls_fingerprint="AB:" * 31 + "AB"
    )
    tv = uploader.connect(cfg)
    assert isinstance(tv, PinnedTV)
    assert tv.art().tls_fingerprint == "ab" * 32
    assert tv._get_rest_api().fingerprint == "ab" * 32
    assert tv.art()._get_rest_api().fingerprint == "ab" * 32


def test_config_round_trip_preserves_pin_and_loads_old_configs(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"host":"tv.test"}')
    cfg = TVConfig.load(path)
    assert cfg.tls_fingerprint is None
    cfg.tls_fingerprint = "ab" * 32
    cfg.save(path)
    assert TVConfig.load(path).tls_fingerprint == "ab" * 32


@pytest.mark.parametrize("value", ["", "abcd", "gg" * 32, "ab" * 31, "ab" * 33])
def test_invalid_fingerprints_are_rejected(value):
    with pytest.raises(ValueError, match="SHA-256"):
        normalize_fingerprint(value)


def test_invalid_fingerprint_is_rejected_before_network_access():
    with pytest.raises(ValueError, match="SHA-256"):
        open_pinned_socket("unused.invalid", 8002, "invalid")


def test_trust_command_only_saves_after_successful_verification(monkeypatch):
    cfg = TVConfig(host="tv.test", tls_fingerprint="ab" * 32)
    saved = []
    monkeypatch.setattr(cli, "_get_config", lambda: cfg)
    monkeypatch.setattr(TVConfig, "save", lambda self: saved.append(self.tls_fingerprint))
    monkeypatch.setattr("sys.argv", ["frame-upload", "trust-tv", "--fingerprint", "cd" * 32])

    def reject(*args, **kwargs):
        raise TVCertificateError("fingerprint mismatch")

    monkeypatch.setattr("frame_uploader.tv_tls.open_pinned_socket", reject)
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2
    assert saved == []
    assert cfg.tls_fingerprint == "ab" * 32

    @contextmanager
    def accept(*args, **kwargs):
        yield

    monkeypatch.setattr("frame_uploader.tv_tls.open_pinned_socket", accept)
    cli.main()
    assert saved == ["cd" * 32]


def test_async_upload_cannot_bypass_saved_pin(monkeypatch):
    monkeypatch.setattr(
        cli, "_get_config", lambda: TVConfig(host="tv.test", tls_fingerprint="ab" * 32)
    )
    monkeypatch.setattr("sys.argv", ["frame-upload", "upload-async", "unused.jpg"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2


def test_inline_thumbnail_uses_authenticated_websocket(certificate):
    cert = certificate()
    with art_server(cert) as server:
        art = PinnedTVArt(
            "localhost", port=server.port, key_press_delay=0, tls_fingerprint=cert.fingerprint
        )
        try:
            assert fetch_thumbnail(art, "MY_test") == (None, bytearray(b"thumbnail"))
            assert len(server.requests) == 1
        finally:
            art.close()


def test_certificate_failure_is_not_misreported_as_art_mode_unavailable(certificate):
    cert = certificate()
    other = certificate()
    with https_server(cert) as server:
        art = PinnedTVArt("localhost", port=server.port, tls_fingerprint=other.fingerprint)
        tv = SimpleNamespace(art=lambda: art)
        with pytest.raises(TVCertificateError):
            uploader.is_in_art_mode(tv)
        assert server.requests == []
