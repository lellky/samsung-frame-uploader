from __future__ import annotations

from dataclasses import replace

import pytest
from PIL import Image

from frame_uploader import app_runner
from frame_uploader.app_config import AppConfig
from frame_uploader.app_db import AppDB
from frame_uploader.optimizer import extract_exif_datetime_from_path, optimize_for_frame


@pytest.fixture
def incoming(tmp_path):
    cfg = replace(
        AppConfig.from_env(),
        input_dir=tmp_path / "incoming",
        optimized_dir=tmp_path / "optimized",
        db_path=tmp_path / "test.sqlite3",
        recursive_scan=True,
        order_mode="exif_date",
    )
    cfg.input_dir.mkdir()
    db = AppDB(cfg.db_path)
    db.init()
    return cfg, db


def save_heic(path, *, orientation=1):
    image = Image.new("RGB", (120, 80), "red")
    image.paste("blue", (60, 0, 120, 80))
    exif = Image.Exif()
    exif[274] = orientation
    exif[306] = "2025:01:02 03:04:05"
    # Real camera capture dates live in the nested Exif IFD.
    exif[0x8769] = {36867: "2020:02:03 04:05:06"}
    image.save(path, format="HEIF", quality=95, exif=exif.tobytes())


@pytest.mark.parametrize("suffix", [".heic", ".HEIC", ".heif", ".HEIF"])
def test_scan_and_optimize_heic_to_uploadable_jpeg(incoming, suffix):
    cfg, db = incoming
    source = cfg.input_dir / f"photo{suffix}"
    save_heic(source)
    original = source.read_bytes()

    assert app_runner.scan_for_new_files(cfg, db) == 1
    pending = list(db.iter_pending_optimize())
    assert len(pending) == 1
    assert pending[0].exif_datetime == "2020-02-03T04:05:06"
    assert app_runner.optimize_pending(cfg, db) == 1
    ready = db.list_cycle_images()
    assert len(ready) == 1
    assert ready[0].exif_datetime == "2020-02-03T04:05:06"
    with Image.open(ready[0].optimized_path) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (3840, 2160)
    assert source.read_bytes() == original

    # Unchanged sources are not optimized again on the next tick.
    app_runner.scan_for_new_files(cfg, db)
    assert app_runner.optimize_pending(cfg, db) == 0


@pytest.mark.parametrize("orientation,top_channel", [(6, 0), (8, 2)])
def test_heic_orientation_is_applied_once(tmp_path, orientation, top_channel):
    source = tmp_path / "portrait.heic"
    save_heic(source, orientation=orientation)
    out, _, _ = optimize_for_frame(source_path=source, optimized_dir=tmp_path / "optimized")
    with Image.open(out) as image:
        top = image.getpixel((1920, 540))
        bottom = image.getpixel((1920, 1620))
        assert top[top_channel] > 200
        assert bottom[2 - top_channel] > 200
        # A rotated portrait is padded on the sides, not above and below.
        assert max(image.getpixel((100, 1080))) < 10


def test_heic_uses_primary_still_image(tmp_path):
    source = tmp_path / "multiple.heic"
    first = Image.new("RGB", (160, 90), "red")
    primary = Image.new("RGB", (160, 90), "blue")
    first.save(source, save_all=True, append_images=[primary], primary_index=1, quality=95)
    out, _, _ = optimize_for_frame(source_path=source, optimized_dir=tmp_path / "optimized")
    with Image.open(out) as image:
        red, _, blue = image.getpixel((1920, 1080))
        assert blue > 200
        assert red < 30


def test_heic_cache_keeps_capture_date(tmp_path):
    source = tmp_path / "photo.heic"
    save_heic(source)
    kwargs = {"source_path": source, "optimized_dir": tmp_path / "optimized"}
    first = optimize_for_frame(**kwargs)
    modified = first[0].stat().st_mtime_ns
    assert optimize_for_frame(**kwargs) == first
    assert first[0].stat().st_mtime_ns == modified
    assert first[2] == "2020-02-03T04:05:06"


def test_corrupt_heic_does_not_block_other_images(incoming):
    cfg, db = incoming
    broken = cfg.input_dir / "broken.heic"
    broken.write_bytes(b"not an image")
    save_heic(cfg.input_dir / "valid.heic")
    assert extract_exif_datetime_from_path(broken) is None
    assert app_runner.scan_for_new_files(cfg, db) == 2
    assert app_runner.optimize_pending(cfg, db) == 1
    failed = list(db.iter_pending_optimize())
    assert len(failed) == 1
    assert failed[0].status == "failed_optimize"
    assert failed[0].last_error


@pytest.mark.parametrize("suffix", [".jpg", ".png"])
def test_jpeg_and_png_still_use_existing_pipeline(incoming, suffix):
    cfg, db = incoming
    Image.new("RGB", (160, 90), "green").save(cfg.input_dir / f"photo{suffix}")
    assert app_runner.scan_for_new_files(cfg, db) == 1
    assert app_runner.optimize_pending(cfg, db) == 1
    with Image.open(db.list_cycle_images()[0].optimized_path) as image:
        assert image.format == "JPEG"
        assert image.size == (3840, 2160)
