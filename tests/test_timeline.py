"""pixelprobe timeline 测试：矩阵形状与取值、排序、范围、CSV、PNG、块模式。"""

from __future__ import annotations

import csv as csv_module
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import (
    COUNTER_POS,
    FLASH_FRAME,
    FRAME_COUNT,
    GREEN_POS,
    run_json,
    run_json_error,
)
from pixelprobe.core import extract_timelines
from pixelprobe.models.errors import InvalidRangeError


def test_single_point_green(test_video: Path) -> None:
    result = extract_timelines(test_video, points=[GREEN_POS])
    assert result.matrix.shape == (1, FRAME_COUNT, 3)
    for t in range(FRAME_COUNT):
        expected = (255, 255, 255) if t == FLASH_FRAME else (0, 255, 0)
        assert tuple(result.matrix[0, t]) == expected
    assert result.frames == list(range(FRAME_COUNT))


def test_counter_pixel_values(test_video: Path) -> None:
    result = extract_timelines(test_video, points=[COUNTER_POS])
    for t in range(FRAME_COUNT):
        expected = (
            (255, 255, 255) if t == FLASH_FRAME
            else (t * 8, t * 4, t * 2)
        )
        assert tuple(result.matrix[0, t]) == expected, f"帧 {t} 取值错误"


def test_multi_point_rows_and_rgb_order(test_video: Path) -> None:
    # 行数 = 选点数，列数 = 分析帧数，行序 = 选择顺序
    points = [(5, 8), GREEN_POS, COUNTER_POS]
    result = extract_timelines(
        test_video, points=points, start_frame=0, end_frame=9
    )
    assert result.matrix.shape == (3, 10, 3)
    # 第 0 行是移动红点路径上的 (5,8)：仅第 5 帧为红
    assert tuple(result.matrix[0, 5]) == (255, 0, 0)
    assert tuple(result.matrix[0, 4]) == (0, 0, 0)
    # 第 1 行绿点：RGB 顺序正确（G 通道 255）
    assert tuple(result.matrix[1, 0]) == (0, 255, 0)


def test_sort_pixel_id(test_video: Path) -> None:
    # (24,16) 的 pixel_id=536，(2,28) 的 pixel_id=898：按 pixel-id 排序后顺序固定
    result = extract_timelines(
        test_video,
        points=[COUNTER_POS, GREEN_POS],
        start_frame=0,
        end_frame=0,
        sort="pixel-id",
    )
    assert [p.pixel_id for p in result.points] == [536, 898]
    assert tuple(result.matrix[0, 0]) == (0, 255, 0)


def test_frame_range_inclusive(test_video: Path) -> None:
    result = extract_timelines(
        test_video, points=[GREEN_POS], start_frame=10, end_frame=20
    )
    assert result.matrix.shape[1] == 11  # 闭区间共 11 帧
    assert result.frames == list(range(10, 21))


def test_sample_every(test_video: Path) -> None:
    result = extract_timelines(
        test_video, points=[GREEN_POS], sample_every=5
    )
    assert result.frames == [0, 5, 10, 15, 20, 25]


def test_time_range(test_video: Path) -> None:
    result = extract_timelines(
        test_video, points=[GREEN_POS], start=0.2, end=0.4
    )
    # 30fps：0.2s → 帧 6，0.4s → 帧 12
    assert result.frames[0] == 6 and result.frames[-1] == 12


def test_mixed_range_rejected(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        extract_timelines(
            test_video, points=[GREEN_POS], start_frame=0, end=0.5
        )


def test_csv_output(test_video: Path, tmp_path: Path) -> None:
    out_csv = tmp_path / "时间线.csv"
    gx, gy = GREEN_POS
    run_json(
        "timeline", test_video,
        "--point", f"{gx},{gy}", "--point", "2,28",
        "--csv", out_csv, "--json",
    )
    with out_csv.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv_module.reader(fh))
    assert rows[0] == ["pixel_id", "x", "y", "frame",
                       "time_seconds", "time_ms", "r", "g", "b"]
    assert len(rows) == 1 + 2 * FRAME_COUNT  # 表头 + K*T 行
    first = rows[1]
    assert first[0] == str(gy * 32 + gx)
    assert [first[6], first[7], first[8]] == ["0", "255", "0"]


def test_png_scale_nearest(test_video: Path, tmp_path: Path) -> None:
    out_png = tmp_path / "tl.png"
    data = run_json(
        "timeline", test_video, "--point", "24,16",
        "--scale", 4, "--output", out_png, "--json",
    )["data"]
    assert data["raw_width"] == FRAME_COUNT and data["raw_height"] == 1
    arr = np.asarray(Image.open(out_png).convert("RGB"))
    assert arr.shape == (4, FRAME_COUNT * 4, 3)
    # 最近邻放大：4×4 方块内颜色一致，无插值
    assert tuple(arr[0, 0]) == (0, 255, 0)
    assert tuple(arr[3, 3]) == (0, 255, 0)
    assert tuple(arr[0, FLASH_FRAME * 4 + 1]) == (255, 255, 255)
    # 同时生成 raw 图
    raw = np.asarray(Image.open(data["raw_output_path"]).convert("RGB"))
    assert raw.shape == (1, FRAME_COUNT, 3)


def test_orientation_vertical(test_video: Path, tmp_path: Path) -> None:
    out_png = tmp_path / "竖排.png"
    data = run_json(
        "timeline", test_video, "--point", "24,16", "--point", "2,28",
        "--orientation", "vertical", "--output", out_png, "--json",
    )["data"]
    assert data["raw_width"] == 2 and data["raw_height"] == FRAME_COUNT


def test_grid_sampling(test_video: Path) -> None:
    result = extract_timelines(
        test_video, grid=(0, 0, 32, 32), step=16,
        start_frame=0, end_frame=0,
    )
    # 32×32 区域步长 16 → 4 个采样点，行优先
    assert [(p.x, p.y) for p in result.points] == [
        (0, 0), (16, 0), (0, 16), (16, 16)
    ]


def test_block_mean_mode(test_video: Path) -> None:
    result = extract_timelines(
        test_video, grid=(0, 0, 32, 32), block_size=16,
        start_frame=0, end_frame=0,
    )
    assert result.sample_type == "block_mean"
    # 帧 0：左上 16×16 块只有一个红点，均值 R = 255/256 ≈ 1（四舍五入）
    assert tuple(result.matrix[0, 0]) == (1, 0, 0)


def test_no_points_is_error(test_video: Path) -> None:
    code, data = run_json_error("timeline", test_video, "--json")
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"
