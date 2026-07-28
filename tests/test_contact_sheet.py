"""contact sheet 采样网格测试：取帧计划、拼图内容、标注。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from conftest import FRAME_COUNT, make_frame, run_json
from pixelprobe.core import sample_frames
from pixelprobe.core.contact_sheet import plan_sheet_frames
from pixelprobe.core.frame_selector import FrameRange


def test_plan_sheet_frames_even_and_deduped() -> None:
    rng = FrameRange(start=0, end=FRAME_COUNT - 1, sample_every=1)
    frames = plan_sheet_frames(rng, 9)
    assert len(frames) == 9
    assert frames[0] == 0 and frames[-1] == FRAME_COUNT - 1
    assert frames == sorted(set(frames))
    # 抽帧数超过总帧数时自动去重
    short = plan_sheet_frames(FrameRange(start=0, end=2, sample_every=1), 9)
    assert short == [0, 1, 2]


def test_sheet_tiles_match_source_frames(test_video: Path) -> None:
    result = sample_frames(
        test_video, count=4, tile_max_dim=32, annotate=False
    )
    assert result.frames == plan_sheet_frames(
        FrameRange(start=0, end=FRAME_COUNT - 1, sample_every=1), 4
    )
    assert (result.cols, result.rows) == (2, 2)
    # 无标注时格子内容与原始帧逐像素一致（32<=tile_max_dim 不缩放）
    tile0 = result.image[0:32, 0:32]
    assert np.array_equal(tile0, make_frame(result.frames[0]))
    x1 = 32 + 2  # 第二列偏移含 2px 间隙
    tile1 = result.image[0:32, x1 : x1 + 32]
    assert np.array_equal(tile1, make_frame(result.frames[1]))


def test_sheet_annotation_bar(test_video: Path) -> None:
    plain = sample_frames(test_video, count=4, tile_max_dim=32, annotate=False)
    labeled = sample_frames(test_video, count=4, tile_max_dim=32, annotate=True)
    # 标注条为每格额外 14px
    assert labeled.image.shape[0] == plain.image.shape[0] + 2 * 14
    bar = labeled.image[32:46, 0:32]
    assert bar.max() > 0  # 黑条上有文字像素


def test_sheet_range_subset(test_video: Path) -> None:
    result = sample_frames(
        test_video, count=3, start_frame=10, end_frame=20, annotate=False
    )
    assert result.frames == [10, 15, 20]


def test_cli_sheet_export(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "网格.png"
    data = run_json(
        "sheet", test_video, "--count", 4, "--output", out, "--json"
    )["data"]
    assert data["cols"] == 2 and data["rows"] == 2
    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == (data["sheet_width"], data["sheet_height"])
