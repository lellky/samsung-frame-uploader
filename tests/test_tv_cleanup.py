from __future__ import annotations

from dataclasses import replace

import pytest
from samsungtvws.exceptions import ResponseError

from frame_uploader import app_runner
from frame_uploader.app_config import AppConfig
from frame_uploader.app_db import AppDB
from frame_uploader.config import TVConfig


class FakeTV:
    def __init__(self):
        self.items = []
        self.current_id = None
        self.list_error = None
        self.failed_ids = set()
        self.delete_attempts = []

    def art(self):
        return self

    def available(self):
        if self.list_error:
            raise self.list_error
        return self.items

    def get_current(self):
        return {"content_id": self.current_id}

    def delete(self, content_id):
        self.delete_attempts.append(content_id)
        if content_id in self.failed_ids or content_id not in {
            item["content_id"] for item in self.items
        }:
            raise ResponseError("`delete_image_list` request failed with error number -7")
        self.items = [item for item in self.items if item["content_id"] != content_id]
        return True


@pytest.fixture
def cleanup(monkeypatch, tmp_path):
    cfg = replace(AppConfig.from_env(), keep_on_tv=2, cleanup_mode="app_only", dry_run=False)
    db = AppDB(tmp_path / "test.sqlite3")
    db.init()
    tv = FakeTV()
    monkeypatch.setattr(TVConfig, "load", lambda: TVConfig(host="tv.test"))
    monkeypatch.setattr(app_runner, "is_tv_reachable", lambda cfg: True)
    monkeypatch.setattr(app_runner, "connect", lambda cfg: tv)

    def record_uploads(*content_ids):
        for cid in content_ids:
            path = tmp_path / f"{cid}.jpg"
            db.upsert_source_file(
                source_path=path,
                mtime_ns=1,
                size=1,
                exif_datetime=None,
                desired_optimizer_version=1,
                desired_resize_mode="pad",
                desired_background_color="black",
            )
            image_id = next(db.iter_images_by_source_prefix(str(path)))[0]
            db.mark_uploaded(image_id=image_id, tv_content_id=cid)

    return cfg, db, tv, record_uploads


def remaining_ids(db):
    return [row.tv_content_id for row in db.iter_uploads_not_deleted()]


def test_cleanup_reconciles_stale_history_before_pruning(cleanup):
    cfg, db, tv, record = cleanup
    record("MY_missing", "MY_old", "MY_recent", "MY_current")
    tv.items = [{"content_id": cid} for cid in ("MY_old", "MY_recent", "MY_current")]
    tv.current_id = "MY_current"

    assert app_runner.cleanup_tv(cfg, db) == 1
    assert tv.delete_attempts == ["MY_old"]
    assert remaining_ids(db) == ["MY_recent", "MY_current"]

    assert app_runner.cleanup_tv(cfg, db) == 0
    assert tv.delete_attempts == ["MY_old"]


def test_cleanup_reconciles_missing_uploads_even_below_limit(cleanup):
    cfg, db, tv, record = cleanup
    record("MY_missing", "MY_present")
    tv.items = [{"content_id": "MY_present"}]

    assert app_runner.cleanup_tv(cfg, db) == 0
    assert remaining_ids(db) == ["MY_present"]
    assert tv.delete_attempts == []


def test_missing_newer_upload_does_not_cause_deletion_of_retained_art(cleanup):
    cfg, db, tv, record = cleanup
    record("MY_present", "MY_missing")
    tv.items = [{"content_id": "MY_present"}]

    assert app_runner.cleanup_tv(replace(cfg, keep_on_tv=1), db) == 0
    assert remaining_ids(db) == ["MY_present"]
    assert tv.delete_attempts == []


def test_cleanup_accepts_empty_inventory(cleanup):
    cfg, db, tv, record = cleanup
    record("MY_missing")

    assert app_runner.cleanup_tv(cfg, db) == 0
    assert remaining_ids(db) == []
    assert tv.delete_attempts == []


@pytest.mark.parametrize("inventory", [None, {}, [None], [{}], [{"content_id": 42}]])
def test_cleanup_ignores_invalid_inventory(cleanup, inventory):
    cfg, db, tv, record = cleanup
    record("MY_present")
    tv.items = inventory

    assert app_runner.cleanup_tv(replace(cfg, keep_on_tv=0), db) == 0
    assert remaining_ids(db) == ["MY_present"]
    assert tv.delete_attempts == []


@pytest.mark.parametrize("error", [OSError("offline"), ResponseError("list failed")])
def test_cleanup_preserves_history_when_listing_fails(cleanup, error):
    cfg, db, tv, record = cleanup
    record("MY_present")
    tv.list_error = error

    assert app_runner.cleanup_tv(replace(cfg, keep_on_tv=0), db) == 0
    assert remaining_ids(db) == ["MY_present"]
    assert tv.delete_attempts == []


def test_cleanup_preserves_history_on_delete_error(cleanup):
    cfg, db, tv, record = cleanup
    record("MY_present")
    tv.items = [{"content_id": "MY_present"}]
    tv.failed_ids.add("MY_present")

    assert app_runner.cleanup_tv(replace(cfg, keep_on_tv=0), db) == 0
    assert remaining_ids(db) == ["MY_present"]
    assert tv.delete_attempts == ["MY_present"]


def test_cleanup_dry_run_does_not_mutate_history_or_tv(cleanup):
    cfg, db, tv, record = cleanup
    record("MY_missing", "MY_present")
    tv.items = [{"content_id": "MY_present"}]

    assert app_runner.cleanup_tv(replace(cfg, keep_on_tv=0, dry_run=True), db) == 1
    assert remaining_ids(db) == ["MY_missing", "MY_present"]
    assert tv.delete_attempts == []


def test_cleanup_preserves_current_art_missing_from_inventory(cleanup):
    cfg, db, tv, record = cleanup
    record("MY_current")
    tv.current_id = "MY_current"

    assert app_runner.cleanup_tv(replace(cfg, keep_on_tv=0), db) == 0
    assert remaining_ids(db) == ["MY_current"]
    assert tv.delete_attempts == []


def test_cleanup_all_my_only_deletes_user_art(cleanup):
    cfg, db, tv, record = cleanup
    tv.items = [{"content_id": cid} for cid in ("SAM_builtin", "MY_old", "MY_current")]
    tv.current_id = "MY_current"

    assert app_runner.cleanup_tv(replace(cfg, cleanup_mode="all_my", keep_on_tv=0), db) == 1
    assert tv.delete_attempts == ["MY_old"]
