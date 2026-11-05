from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from sqlite3 import IntegrityError
from types import SimpleNamespace

import pytest
from samsungtvws.exceptions import ResponseError

from frame_uploader import app_runner
from frame_uploader.app_config import AppConfig
from frame_uploader.app_db import AppDB
from frame_uploader.config import TVConfig


@pytest.fixture
def rotation(monkeypatch, tmp_path):
    db = AppDB(tmp_path / "test.sqlite3")
    db.init()
    cfg = replace(
        AppConfig.from_env(),
        order_mode="created_at",
        order_seed="rotation-test",
        upload_interval_seconds=0,
        select_after_upload=True,
        dry_run=False,
        cleanup_mode="app_only",
        keep_on_tv=20,
    )
    tv = SimpleNamespace(
        items={}, uploads=[], attempts=[], selected=[], current_id=None, select_error=None
    )

    def add(name, *, previously_uploaded=False):
        source = tmp_path / f"{name}.jpg"
        source.write_bytes(name.encode())
        db.upsert_source_file(
            source_path=source,
            mtime_ns=1,
            size=len(name),
            exif_datetime=None,
            desired_optimizer_version=1,
            desired_resize_mode="pad",
            desired_background_color="black",
        )
        image_id = next(db.iter_images_by_source_prefix(str(source)))[0]
        db.mark_optimized(
            image_id=image_id,
            sha256=name,
            optimized_path=source,
            exif_datetime=f"2020:01:{image_id:02d} 00:00:00",
            optimizer_version=1,
            optimized_resize_mode="pad",
            optimized_background_color="black",
        )
        if previously_uploaded:
            content_id = f"MY_existing_{name}"
            tv.items[content_id] = name
            db.mark_uploaded(image_id=image_id, tv_content_id=content_id)
        return image_id

    def upload(client, path, **kwargs):
        name = Path(path).stem
        tv.uploads.append(name)
        content_id = f"MY_upload_{len(tv.uploads)}"
        tv.items[content_id] = name
        return content_id

    def select(client, content_id):
        tv.attempts.append(content_id)
        if tv.select_error:
            raise tv.select_error
        if content_id not in tv.items:
            raise ResponseError("content missing")
        tv.selected.append(tv.items[content_id])
        tv.current_id = content_id

    def delete(client, content_id):
        del tv.items[content_id]

    monkeypatch.setattr(TVConfig, "load", lambda: TVConfig(host="tv.test"))
    monkeypatch.setattr(app_runner, "is_tv_reachable", lambda cfg: True)
    monkeypatch.setattr(app_runner, "connect", lambda cfg: tv)
    monkeypatch.setattr(app_runner, "upload_image", upload)
    monkeypatch.setattr(app_runner, "select_image", select)
    monkeypatch.setattr(
        app_runner, "list_art", lambda client: [{"content_id": c} for c in tv.items]
    )
    monkeypatch.setattr(app_runner, "get_current", lambda client: {"content_id": tv.current_id})
    monkeypatch.setattr(app_runner, "delete_image", delete)
    return SimpleNamespace(cfg=cfg, db=db, tv=tv, add=add)


def test_new_image_interrupts_and_resumes_existing_round(rotation):
    r = rotation
    ids = {name: r.add(name, previously_uploaded=True) for name in "ABCDE"}
    r.db.set_state("cycle_last_image_id", str(ids["B"]))
    r.add("F")

    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.selected == ["F"]
    assert r.db.get_state("cycle_last_image_id") == str(ids["B"])

    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.selected == ["F", "C"]


def test_continuous_arrivals_leave_slots_for_existing_images(rotation):
    r = rotation
    ids = {name: r.add(name, previously_uploaded=True) for name in "ABCDE"}
    r.db.set_state("cycle_last_image_id", str(ids["E"]))

    for name in "FGHIJ":
        r.add(name)
        app_runner.upload_one(r.cfg, r.db)
        app_runner.upload_one(r.cfg, r.db)

    assert r.tv.selected == list("FAGBHCIDJE")


@pytest.mark.parametrize("order_mode", ["created_at", "filename", "exif_date", "random"])
def test_existing_round_finishes_even_when_collection_grows_every_turn(rotation, order_mode):
    r = rotation
    cfg = replace(r.cfg, order_mode=order_mode)
    ids = {name: r.add(name, previously_uploaded=True) for name in "ABCDE"}
    r.db.set_state("cycle_last_image_id", str(ids["E"]))
    for index in range(15):
        r.add(f"new{index:02d}")
        app_runner.upload_one(cfg, r.db)
        app_runner.upload_one(cfg, r.db)
    counts = Counter(r.tv.selected)
    assert all(counts[name] >= 2 for name in "ABCDE")


def test_new_image_and_round_position_survive_restart(rotation):
    r = rotation
    for name in "ABCDE":
        r.add(name, previously_uploaded=True)
    app_runner.upload_one(r.cfg, r.db)
    r.add("F")
    app_runner.upload_one(r.cfg, r.db)

    restarted = AppDB(r.db.db_path)
    restarted.init()
    app_runner.upload_one(r.cfg, restarted)
    assert r.tv.selected == ["A", "F", "B"]


def test_removed_round_members_do_not_skip_surviving_images(rotation):
    r = rotation
    ids = {name: r.add(name, previously_uploaded=True) for name in "ABC"}
    app_runner.upload_one(r.cfg, r.db)
    r.db.mark_source_missing(image_id=ids["B"], source_path="removed")
    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.selected == ["A", "C"]


@pytest.mark.parametrize("order_mode", ["created_at", "filename", "exif_date", "random"])
def test_static_collection_visits_each_image_once_per_round(rotation, order_mode):
    r = rotation
    for name in "ABCDE":
        r.add(name, previously_uploaded=True)
    cfg = replace(r.cfg, order_mode=order_mode)
    for _ in range(20):
        app_runner.upload_one(cfg, r.db)
    assert Counter(r.tv.selected) == {name: 4 for name in "ABCDE"}
    assert r.tv.selected[:5] == r.tv.selected[5:10] == r.tv.selected[10:15] == r.tv.selected[15:]


@pytest.mark.parametrize(
    "error",
    [app_runner.ArtModeRequiredError("not in art mode"), ResponseError("busy"), OSError("offline")],
)
def test_failed_selection_retries_existing_upload_after_restart(rotation, monkeypatch, error):
    r = rotation
    a = r.add("A", previously_uploaded=True)
    b = r.add("B", previously_uploaded=True)
    r.db.set_state("cycle_last_image_id", str(a))
    r.tv.select_error = error

    content_id = app_runner.upload_one(r.cfg, r.db)
    assert r.tv.uploads == ["B"]
    assert r.tv.selected == []
    assert r.db.get_state("cycle_last_image_id") == str(a)

    r.add("C")
    restarted = AppDB(r.db.db_path)
    restarted.init()
    r.tv.select_error = None
    # Retrying the selection must not wait for the next upload interval.
    monkeypatch.setattr(app_runner, "_should_upload_now", lambda cfg, db: False)
    assert app_runner.upload_one(r.cfg, restarted) is None
    assert r.tv.attempts == [content_id, content_id]
    assert r.tv.uploads == ["B"]
    assert r.tv.selected == ["B"]
    assert restarted.get_state("cycle_last_image_id") == str(b)


def test_failed_new_image_selection_preserves_existing_round(rotation):
    r = rotation
    ids = {name: r.add(name, previously_uploaded=True) for name in "ABCDE"}
    r.db.set_state("cycle_last_image_id", str(ids["B"]))
    r.add("F")
    r.tv.select_error = ResponseError("busy")
    app_runner.upload_one(r.cfg, r.db)

    r.tv.select_error = None
    app_runner.upload_one(r.cfg, r.db)
    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.uploads == ["F", "C"]
    assert r.tv.selected == ["F", "C"]


def test_failed_selection_of_previously_pruned_image_does_not_upload_twice(rotation):
    r = rotation
    r.add("A", previously_uploaded=True)
    r.db.mark_upload_tv_deleted(tv_content_id="MY_existing_A")
    del r.tv.items["MY_existing_A"]
    r.tv.select_error = ResponseError("busy")
    app_runner.upload_one(r.cfg, r.db)

    r.tv.select_error = None
    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.uploads == ["A"]
    assert r.tv.selected == ["A"]


@pytest.mark.parametrize("cleanup_mode", ["app_only", "all_my"])
def test_cleanup_preserves_upload_awaiting_selection(rotation, cleanup_mode):
    r = rotation
    r.add("A")
    r.tv.select_error = ResponseError("busy")
    content_id = app_runner.upload_one(r.cfg, r.db)

    assert app_runner.cleanup_tv(replace(r.cfg, keep_on_tv=0, cleanup_mode=cleanup_mode), r.db) == 0
    assert content_id in r.tv.items
    r.tv.select_error = None
    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.selected == ["A"]
    assert r.tv.uploads == ["A"]


def test_pending_selection_missing_from_tv_is_uploaded_again(rotation):
    r = rotation
    r.add("A")
    r.tv.select_error = ResponseError("busy")
    content_id = app_runner.upload_one(r.cfg, r.db)
    del r.tv.items[content_id]
    app_runner.cleanup_tv(r.cfg, r.db)

    r.tv.select_error = None
    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.uploads == ["A", "A"]
    assert r.tv.selected == ["A"]


def test_pending_selection_of_removed_source_is_abandoned(rotation):
    r = rotation
    a = r.add("A")
    r.tv.select_error = ResponseError("busy")
    app_runner.upload_one(r.cfg, r.db)
    source = next(r.db.iter_images_by_source_prefix(str(r.db.db_path.parent / "A.jpg")))[1]
    r.db.mark_source_missing(image_id=a, source_path=source)
    r.add("B")
    r.tv.select_error = None

    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.selected == ["B"]


def test_upload_only_mode_completes_rotation_without_selecting(rotation):
    r = rotation
    a = r.add("A", previously_uploaded=True)
    r.add("B", previously_uploaded=True)
    app_runner.upload_one(replace(r.cfg, select_after_upload=False), r.db)
    assert r.tv.uploads == ["A"]
    assert r.tv.attempts == []
    assert r.db.get_state("cycle_last_image_id") == str(a)


def test_dry_run_does_not_retry_or_complete_pending_selection(rotation):
    r = rotation
    r.add("A")
    r.tv.select_error = ResponseError("busy")
    app_runner.upload_one(r.cfg, r.db)
    r.tv.select_error = None

    app_runner.upload_one(replace(r.cfg, dry_run=True), r.db)
    assert len(r.tv.attempts) == 1
    assert r.tv.selected == []
    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.selected == ["A"]
    assert r.tv.uploads == ["A"]


def test_retried_selection_gets_full_display_interval(rotation):
    r = rotation
    r.add("A")
    r.tv.select_error = ResponseError("busy")
    app_runner.upload_one(r.cfg, r.db)
    with closing(r.db._connect()) as conn, conn:
        conn.execute("UPDATE uploads SET uploaded_at = '2000-01-01T00:00:00+00:00'")
    r.tv.select_error = None

    cfg = replace(r.cfg, upload_interval_seconds=300)
    app_runner.upload_one(cfg, r.db)
    assert r.tv.uploads == ["A"]
    assert r.tv.selected == ["A"]
    assert not app_runner._should_upload_now(cfg, r.db)


def test_failed_progress_commit_retains_pending_selection_and_old_cursor(rotation):
    r = rotation
    a = r.add("A", previously_uploaded=True)
    b = r.add("B", previously_uploaded=True)
    r.db.set_state("cycle_last_image_id", str(a))
    with closing(r.db._connect()) as conn, conn:
        conn.execute(
            """
            CREATE TRIGGER fail_progress BEFORE INSERT ON state
            WHEN NEW.key = 'rotation_last_kind'
            BEGIN SELECT RAISE(ABORT, 'synthetic completion failure'); END
            """
        )
    with pytest.raises(IntegrityError, match="synthetic completion failure"):
        app_runner.upload_one(r.cfg, r.db)
    assert r.db.get_state("cycle_last_image_id") == str(a)

    with closing(r.db._connect()) as conn, conn:
        conn.execute("DROP TRIGGER fail_progress")
    app_runner.upload_one(r.cfg, r.db)
    assert r.tv.uploads == ["B"]
    assert r.db.get_state("cycle_last_image_id") == str(b)
