"""pixelprobe xt 测试：X–T 切片与原视频逐像素对应。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from conftest import (
    FLASH_FRAME,
    FRAME_COUNT,
    RED_Y,
    make_frame,
    run_json,
    run_json_error,
)
from pixelprobe.core import create_xt_slice


def test_xt_matches_source_video(test_video: Path) -> None:
    result = create_xt_slice(test_video, RED_Y)
    assert result.array.shape == (FRAME_COUNT, 32, 3)
    expected = np.stack(
        [make_frame(t)[RED_Y, :, :] for t in range(FRAME_COUNT)], axis=0
    )
    assert np.array_equal(result.array, expected)


def test_xt_moving_pixel_diagonal(test_video: Path) -> None:
    result = create_xt_slice(test_video, RED_Y)
    for t in range(FRAME_COUNT):
        if t == FLASH_FRAME:
            assert (result.array[t] == 255).all()
        else:
            assert tuple(result.array[t, t]) == (255, 0, 0)


def test_xt_range_and_sampling(test_video: Path) -> None:
    result = create_xt_slice(
        test_video, RED_Y, start_frame=4, end_frame=12, sample_every=2
    )
    assert result.frames == [4, 6, 8, 10, 12]
    assert result.array.shape == (5, 32, 3)
    assert tuple(result.array[0, 4]) == (255, 0, 0)


def test_xt_cli_output_and_metadata(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "xt.png"
    data = run_json(
        "xt", test_video, "--y", RED_Y,
        "--scale-x", 2, "--scale-t", 3, "--output", out, "--json",
    )["data"]
    assert data["slice_type"] == "xt"
    assert data["space_axis"] == "original_x"
    assert data["time_axis"] == "vertical"
    assert data["width"] == 32 * 2 and data["height"] == FRAME_COUNT * 3
    arr = np.asarray(Image.open(out).convert("RGB"))
    assert arr.shape == (FRAME_COUNT * 3, 32 * 2, 3)
    # 帧 1 的红点 x=1：放大后位于行 3~5、列 2~3
    assert tuple(arr[3, 2]) == (255, 0, 0)
    assert tuple(arr[5, 3]) == (255, 0, 0)


def test_xt_y_out_of_range(test_video: Path) -> None:
    code, data = run_json_error("xt", test_video, "--y", 32, "--json")
    assert code == 5
    assert data["error"]["code"] == "COORDINATE_OUT_OF_RANGE"
