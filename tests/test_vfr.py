"""可变帧率（VFR）精确寻址测试：PTS 索引路径。"""

from __future__ import annotations

import bisect
from pathlib import Path

import numpy as np

from conftest import VFR_FRAME_COUNT, run_json, vfr_frame
from pixelprobe.core import VideoReader, extract_timelines, get_media_info


def test_pts_index_exact_count(vfr_video: Path) -> None:
    with VideoReader() as reader:
        reader.open(vfr_video)
        index = reader.build_pts_index()
        assert index is not None and len(index) == VFR_FRAME_COUNT
        assert index == sorted(index)
        count, estimated = reader.frame_count()
        assert count == VFR_FRAME_COUNT and estimated is False


def test_frame_identity_by_index(vfr_video: Path) -> None:
    # 每帧纯色唯一，可断言帧身份：不均匀 PTS 下帧号仍精确
    with VideoReader() as reader:
        reader.open(vfr_video)
        for idx in (0, 1, 5, 10, VFR_FRAME_COUNT - 1):
            _t, arr = reader.get_frame_by_index(idx)
            assert np.array_equal(arr, vfr_frame(idx)), f"帧 {idx} 身份错误"


def test_time_maps_to_last_frame_not_after(vfr_video: Path) -> None:
    with VideoReader() as reader:
        reader.open(vfr_video)
        index = reader.build_pts_index()
        assert index is not None
        tb = float(reader.time_base)
        # 落在两帧之间的时间应取前一帧
        mid = (index[2] * tb + index[3] * tb) / 2
        idx, t, arr = reader.get_frame_by_time(mid)
        assert idx == 2
        assert np.array_equal(arr, vfr_frame(2))
        # 恰好落在帧时间戳上取该帧
        idx2, _, _ = reader.get_frame_by_time(index[4] * tb)
        assert idx2 == 4


def test_iter_frames_range(vfr_video: Path) -> None:
    with VideoReader() as reader:
        reader.open(vfr_video)
        out = list(reader.iter_frames(3, 8))
        assert [i for i, _, _ in out] == [3, 4, 5, 6, 7, 8]
        for i, _, arr in out:
            assert np.array_equal(arr, vfr_frame(i))
        # 时间严格递增（真实 PTS）
        times = [t for _, t, _ in out]
        assert times == sorted(times) and len(set(times)) == len(times)


def test_timeline_on_vfr(vfr_video: Path) -> None:
    result = extract_timelines(vfr_video, points=[(0, 0)])
    assert result.matrix.shape == (1, VFR_FRAME_COUNT, 3)
    for t in range(VFR_FRAME_COUNT):
        assert tuple(result.matrix[0, t]) == tuple(vfr_frame(t)[0, 0])


def test_cli_frame_on_vfr(vfr_video: Path) -> None:
    data = run_json("frame", vfr_video, "--frame", 7, "--json")["data"]
    assert data["frame"] == 7
    pix = run_json(
        "pixel", vfr_video, "--frame", 7, "--point", "0,0", "--json"
    )["data"]["pixels"][0]["rgb"]
    assert (pix["r"], pix["g"], pix["b"]) == (84, 100, 171)


def test_nonzero_pts_uses_zero_based_public_time(offset_vfr_video: Path) -> None:
    info = get_media_info(offset_vfr_video)
    assert info.duration_seconds is not None and info.duration_seconds < 2

    with VideoReader() as reader:
        reader.open(offset_vfr_video)
        index = reader.build_pts_index()
        assert index is not None and index[0] > 0
        times = reader.frame_timestamps()
        assert times[0] == 0.0
        assert times == sorted(times)
        target = (times[9] + times[10]) / 2
        idx, actual_time, arr = reader.get_frame_by_time(target)

    expected = bisect.bisect_right(times, target) - 1
    assert idx == expected
    assert actual_time == times[expected]
    assert np.array_equal(arr, vfr_frame(expected))


def test_open_resets_pts_index(
    test_video: Path, offset_vfr_video: Path,
) -> None:
    with VideoReader() as reader:
        reader.open(test_video)
        first_index = reader.build_pts_index()
        assert first_index is not None and len(first_index) != VFR_FRAME_COUNT

        reader.open(offset_vfr_video)
        second_index = reader.build_pts_index()
        assert second_index is not None and len(second_index) == VFR_FRAME_COUNT
        assert second_index[0] != first_index[0]
