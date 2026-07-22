"""pixelprobe yt 测试：Y–T 切片与原视频逐像素对应。"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from conftest import (
    FLASH_FRAME,
    FRAME_COUNT,
    GREEN_POS,
    make_frame,
    run_json,
    run_json_error,
)
from pixelprobe.core import create_yt_slice


def test_yt_matches_source_video(test_video: Path) -> None:
    gx, gy = GREEN_POS
    result = create_yt_slice(test_video, gx)
    assert result.array.shape == (FRAME_COUNT, 32, 3)
    expected = np.stack(
        [make_frame(t)[:, gx, :] for t in range(FRAME_COUNT)], axis=0
    )
    assert np.array_equal(result.array, expected)


def test_yt_fixed_green_column(test_video: Path) -> None:
    gx, gy = GREEN_POS
    result = create_yt_slice(test_video, gx)
    for t in range(FRAME_COUNT):
        if t == FLASH_FRAME:
            assert (result.array[t] == 255).all()
        else:
            # 横轴是原视频 y：绿点固定出现在列 gy
            assert tuple(result.array[t, gy]) == (0, 255, 0)


def test_yt_cli_metadata(test_video: Path, tmp_path: Path) -> None:
    gx, _ = GREEN_POS
    out = tmp_path / "yt.png"
    data = run_json(
        "yt", test_video, "--x", gx, "--output", out, "--json"
    )["data"]
    assert data["slice_type"] == "yt"
    assert data["space_axis"] == "original_y"
    assert data["time_axis"] == "vertical"
    assert data["width"] == 32 and data["height"] == FRAME_COUNT
    assert out.exists()


def test_yt_x_out_of_range(test_video: Path) -> None:
    code, data = run_json_error("yt", test_video, "--x", -1, "--json")
    assert code == 5
    assert data["error"]["code"] == "COORDINATE_OUT_OF_RANGE"
