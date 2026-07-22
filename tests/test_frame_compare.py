"""compare_frames 两帧比较测试：变化像素定位、bbox、参数校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from conftest import COUNTER_POS, GREEN_POS, RED_Y, run_json, run_json_error
from pixelprobe.core import compare_frames
from pixelprobe.models.errors import InvalidRangeError


def test_locates_changed_pixels_and_bbox(test_video: Path) -> None:
    result = compare_frames(test_video, frame_a=0, frame_b=5, threshold=10)
    # 变化像素：红点旧位 (0,8)、红点新位 (5,8)、计数像素 (2,28)
    assert result.changed_pixels == 3
    cx, cy = COUNTER_POS
    assert result.bbox == (0, RED_Y, 6, cy - RED_Y + 1)
    assert result.max_abs_diff == 255
    assert result.frame_a == 0 and result.frame_b == 5


def test_rect_limits_comparison(test_video: Path) -> None:
    gx, gy = GREEN_POS
    result = compare_frames(
        test_video, frame_a=0, frame_b=5, rect=(gx, gy, 1, 1)
    )
    # 绿像素两帧一致：无变化
    assert result.changed_pixels == 0
    assert result.bbox is None
    assert result.mean_abs_diff == 0.0


def test_time_based_selection(test_video: Path) -> None:
    # 30fps：0.1 秒 → 第 3 帧
    result = compare_frames(test_video, time_a=0.0, frame_b=3)
    assert result.frame_a == 0
    assert result.frame_b == 3
    assert result.time_b == pytest.approx(0.1, abs=0.01)


def test_requires_exactly_one_selector(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        compare_frames(test_video, frame_a=0, time_a=0.5, frame_b=1)
    with pytest.raises(InvalidRangeError):
        compare_frames(test_video, frame_a=0)  # 帧 B 未指定


def test_threshold_range_validated(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        compare_frames(test_video, frame_a=0, frame_b=1, threshold=300)


def test_cli_compare_export(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "差异.png"
    data = run_json(
        "compare", test_video, "--frame-a", 0, "--frame-b", 5,
        "--output", out, "--json",
    )["data"]
    assert data["changed_pixels"] == 3
    assert data["bbox"]["x"] == 0
    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == (32, 32)


def test_cli_compare_missing_selector(test_video: Path) -> None:
    code, data = run_json_error(
        "compare", test_video, "--frame-a", 0, "--json"
    )
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"
