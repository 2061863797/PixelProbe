"""媒体分层索引：与直接解码逐值一致、失效、容量上限、并发提交与清理保护。"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from conftest import FRAME_COUNT, run_json, run_json_error
from pixelprobe.core import detect_changes, scan_media
from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.engine.cache_lifecycle import CacheInUseError, reference
from pixelprobe.index import IndexStore, build_index, full_frame_changes
from pixelprobe.models.errors import UnsupportedMediaError


@pytest.fixture()
def video_copy(test_video: Path, tmp_path: Path) -> Path:
    """每个测试用独立副本，修改文件不影响其他测试。"""
    target = tmp_path / "副本.mkv"
    shutil.copyfile(test_video, target)
    return target


@pytest.fixture()
def store(tmp_path: Path) -> IndexStore:
    return IndexStore(tmp_path / "索引")


def test_build_covers_every_frame_and_segments_tile_the_timeline(
    video_copy: Path, store: IndexStore,
) -> None:
    index = store.save(video_copy, build_index(video_copy))
    frames = index.frames
    assert index.frame_count == FRAME_COUNT
    assert np.array_equal(frames["frame"], np.arange(FRAME_COUNT))
    assert np.isnan(frames["diff_prev"][0]) and not np.isnan(frames["diff_prev"][1:]).any()
    segments = index.segments
    assert segments[0]["start_frame"] == 0
    assert segments[-1]["end_frame"] == FRAME_COUNT - 1
    for before, after in zip(segments, segments[1:]):
        assert before["end_frame"] == after["start_frame"]
    assert any(segment["kind"] == "change" for segment in segments)
    thumbs, thumb_frames = index.thumbs()
    assert thumbs.dtype == np.uint8 and thumbs.shape[0] == len(thumb_frames)
    summary = index.summary()
    assert summary["frame_count"] == FRAME_COUNT and summary["size_bytes"] > 0


def test_index_changes_match_decoding_exactly(video_copy: Path, store: IndexStore) -> None:
    index = store.save(video_copy, build_index(video_copy))
    decoded = detect_changes(video_copy)
    indexed = detect_changes(video_copy, index=index)
    assert indexed.computed_from == "index" and decoded.computed_from == "decode"
    assert indexed.records == decoded.records
    assert indexed.frames_analyzed == decoded.frames_analyzed
    part = detect_changes(video_copy, start_frame=4, end_frame=20, index=index)
    assert part.computed_from == "index"
    assert part.records == detect_changes(video_copy, start_frame=4, end_frame=20).records


def test_index_is_not_used_when_it_cannot_be_exact(video_copy: Path, store: IndexStore) -> None:
    index = store.save(video_copy, build_index(video_copy))
    assert full_frame_changes(index, FrameRange(0, 10, 2)) is None
    assert detect_changes(video_copy, sample_every=2, index=index).computed_from == "decode"
    assert detect_changes(video_copy, rect=(0, 0, 8, 8), index=index).computed_from == "decode"
    # 要求的帧超出索引范围（索引不完整）时同样退回解码
    assert full_frame_changes(index, FrameRange(0, FRAME_COUNT + 5, 1)) is None


def test_index_scan_matches_decoding_including_sheet_pixels(
    video_copy: Path, store: IndexStore,
) -> None:
    index = store.save(video_copy, build_index(video_copy))
    decoded = scan_media(video_copy, sheet_count=4)
    indexed = scan_media(video_copy, sheet_count=4, index=index)
    assert indexed.computed_from == "index"
    assert indexed.records == decoded.records
    assert indexed.anomalies == decoded.anomalies
    assert indexed.events == decoded.events
    assert indexed.frames_analyzed == decoded.frames_analyzed
    assert indexed.sheet.frames == decoded.sheet.frames
    assert np.array_equal(indexed.sheet.image, decoded.sheet.image)
    assert scan_media(video_copy, sample_every=3, index=index).computed_from == "decode"


def test_modified_media_invalidates_index(video_copy: Path, store: IndexStore) -> None:
    store.save(video_copy, build_index(video_copy))
    assert store.lookup(video_copy) is not None
    with video_copy.open("ab") as handle:
        handle.write(b"\0")
    assert store.lookup(video_copy) is None


def test_thumbnail_budget_keeps_cube_bounded(video_copy: Path) -> None:
    single = 32 * 32 * 3
    built = build_index(video_copy, thumb_budget_bytes=single * 7)
    assert built.thumbs.nbytes <= single * 7
    assert built.thumb_stride > 1
    assert all(frame % built.thumb_stride == 0 for frame in built.thumb_frames.tolist())


def test_concurrent_save_keeps_one_valid_copy(video_copy: Path, store: IndexStore) -> None:
    built = build_index(video_copy)
    first = store.save(video_copy, built)
    second = store.save(video_copy, built)
    assert first.directory == second.directory
    assert len(store.entries()) == 1
    assert not [path for path in store.root.iterdir() if path.name.startswith(".tmp-")]


def test_remove_and_prune_respect_active_references(video_copy: Path, store: IndexStore) -> None:
    store.save(video_copy, build_index(video_copy))
    preview = store.prune(max_age_seconds=None, dry_run=True)
    assert preview["entries"] == 1 and len(store.entries()) == 1
    lease = reference(store.root)
    try:
        with pytest.raises(CacheInUseError):
            store.remove(video_copy)
    finally:
        lease.close()
    assert store.remove(video_copy) is True
    assert store.lookup(video_copy) is None
    assert store.remove(video_copy) is False


def test_corrupt_index_is_ignored(video_copy: Path, store: IndexStore) -> None:
    index = store.save(video_copy, build_index(video_copy))
    (index.directory / "frames.npz").write_bytes(b"broken")
    assert store.lookup(video_copy) is None
    # 重新建立时替换损坏的那一份
    assert store.save(video_copy, build_index(video_copy)) is not None


def test_images_are_not_indexed(test_image: Path) -> None:
    with pytest.raises(UnsupportedMediaError):
        build_index(test_image)


def test_cli_index_lifecycle_and_transparent_use(video_copy: Path, tmp_path: Path) -> None:
    index_dir = tmp_path / "cli-index"
    common = ("--index-dir", index_dir, "--json")
    assert run_json("index", "show", video_copy, *common)["data"]["status"] == "missing"
    decoded = run_json("changes", video_copy, *common)["data"]
    built = run_json("index", "build", video_copy, *common)["data"]
    assert built["status"] == "built" and built["frame_count"] == FRAME_COUNT
    assert run_json("index", "build", video_copy, *common)["data"]["status"] == "existing"
    indexed = run_json("changes", video_copy, *common)["data"]
    assert indexed.pop("computed_from") == "index"
    assert decoded.pop("computed_from") == "decode"
    assert indexed == decoded
    forced = run_json("changes", video_copy, "--no-index", *common)["data"]
    assert forced["computed_from"] == "decode"
    shown = run_json("index", "show", video_copy, "--segments", *common)["data"]
    assert shown["status"] == "ready" and shown["segments"]
    pruned = run_json("index", "prune", "--all", "--apply", *common)["data"]
    assert pruned["entries"] == 1
    assert IndexStore(index_dir).entries() == []
    assert run_json("index", "show", video_copy, *common)["data"]["status"] == "missing"


def test_cli_index_build_rejects_images(test_image: Path, tmp_path: Path) -> None:
    code, data = run_json_error(
        "index", "build", test_image, "--index-dir", tmp_path / "i", "--json",
    )
    assert code == 4 and data["error"]["code"] == "UNSUPPORTED_MEDIA"
