"""pixelprobe region 测试：统计正确性、越界、裁剪导出。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from conftest import make_image_array, run_json, run_json_error
from pixelprobe.core import analyze_region


def test_region_stats_small_rect(test_image: Path) -> None:
    # 左上 2×2：像素 (0,0)=(0,0,0) (1,0)=(16,0,8) (0,1)=(0,16,8) (1,1)=(16,16,16)
    stats = run_json(
        "region", test_image, "--rect", "0,0,2,2", "--json"
    )["data"]["statistics"]
    assert stats["pixel_count"] == 4
    assert stats["mean_rgb"] == {"r": 8.0, "g": 8.0, "b": 8.0}
    assert stats["median_rgb"] == {"r": 8.0, "g": 8.0, "b": 8.0}
    assert stats["min_rgb"] == {"r": 0.0, "g": 0.0, "b": 0.0}
    assert stats["max_rgb"] == {"r": 16.0, "g": 16.0, "b": 16.0}
    assert stats["std_rgb"]["r"] == 8.0


def test_region_full_image_matches_numpy(test_image: Path) -> None:
    stats = analyze_region(make_image_array(), (0, 0, 16, 16))
    expected = make_image_array().reshape(-1, 3).astype(np.float64)
    np.testing.assert_allclose(
        [stats.mean_rgb.r, stats.mean_rgb.g, stats.mean_rgb.b],
        expected.mean(axis=0),
        atol=1e-3,
    )
    np.testing.assert_allclose(
        [stats.std_rgb.r, stats.std_rgb.g, stats.std_rgb.b],
        expected.std(axis=0),
        atol=1e-3,
    )
    assert stats.pixel_count == 256


def test_region_video_flash_frame(test_video: Path) -> None:
    stats = run_json(
        "region", test_video, "--frame", 15, "--rect", "4,4,10,10", "--json"
    )["data"]["statistics"]
    assert stats["mean_rgb"] == {"r": 255.0, "g": 255.0, "b": 255.0}
    assert stats["std_rgb"] == {"r": 0.0, "g": 0.0, "b": 0.0}
    assert stats["mean_luminance"] == 255.0


def test_region_out_of_bounds(test_image: Path) -> None:
    code, data = run_json_error(
        "region", test_image, "--rect", "10,10,10,10", "--json"
    )
    assert code == 5
    assert data["error"]["code"] == "COORDINATE_OUT_OF_RANGE"


def test_region_output_crop(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "区域.png"
    data = run_json(
        "region", test_video, "--frame", 0,
        "--rect", "0,8,4,1", "--output-crop", out, "--json",
    )["data"]
    assert data["crop_path"] == str(out)
    arr = np.asarray(Image.open(out).convert("RGB"), dtype=np.uint8)
    assert arr.shape == (1, 4, 3)
    # 第 0 帧红点在 (0,8)
    assert tuple(arr[0, 0]) == (255, 0, 0)
    assert tuple(arr[0, 1]) == (0, 0, 0)
