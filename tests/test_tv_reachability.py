from __future__ import annotations

from frame_uploader import uploader
from frame_uploader.config import TVConfig


class _DummySocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


def test_is_tv_reachable_false_when_host_missing() -> None:
    cfg = TVConfig(host="", port=8002)
    assert uploader.is_tv_reachable(cfg) is False


def test_is_tv_reachable_true_when_tcp_connect_succeeds(monkeypatch) -> None:  # noqa: ANN001
    calls: dict = {}

    def fake_create_connection(addr, timeout=None, **kwargs):  # noqa: ANN001
        calls["addr"] = addr
        calls["timeout"] = timeout
        calls["kwargs"] = kwargs
        return _DummySocket()

    monkeypatch.setattr(uploader.socket, "create_connection", fake_create_connection)

    cfg = TVConfig(host="1.2.3.4", port=8002)
    assert uploader.is_tv_reachable(cfg, timeout=0.25) is True
    assert calls["addr"] == ("1.2.3.4", 8002)
    assert calls["timeout"] == 0.25


def test_is_tv_reachable_false_when_tcp_connect_raises(monkeypatch) -> None:  # noqa: ANN001
    def fake_create_connection(*args, **kwargs):  # noqa: ANN001
        raise OSError("boom")

    monkeypatch.setattr(uploader.socket, "create_connection", fake_create_connection)

    cfg = TVConfig(host="1.2.3.4", port=8002)
    assert uploader.is_tv_reachable(cfg, timeout=0.25) is False
