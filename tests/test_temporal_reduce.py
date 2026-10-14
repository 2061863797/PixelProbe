"""temporal_reduce 时间域合成测试：藏图显形、统计正确性、median 内存守护。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import (
    FRAME_COUNT,
    GREEN_POS,
    NOISE_QUIET_RECT,
    NOISE_SIZE,
    run_json,
    run_json_error,
)
from types import SimpleNamespace

import pixelprobe
from pixelprobe.domain.errors import MaterializationLimitExceededError
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.operators.base import ResourcePolicy
from pixelprobe.request_builders import reduce_request


def temporal_reduce(
    path: Path, op: str = "std", rect=None, max_memory_bytes: int | None = None, **options,
) -> SimpleNamespace:
    """经引擎做时间域合成，按 CLI 的方式汇总统计量与预览拉伸端点。"""
    request = reduce_request(path, operation=op, rect=rect, **options)
    if max_memory_bytes is not None:
        request = request.model_copy(
            update={"resources": ResourcePolicy(max_memory_bytes=max_memory_bytes)},
        )
    data, preview = pixelprobe.generate(request).request_tensors[0]
    statistic = data.data.materialize()

    def summary(values) -> list[float]:
        return [round(float(value), 4) for value in values]

    return SimpleNamespace(
        image=preview.data.materialize(),
        stat_min=summary(statistic.min(axis=(0, 1))),
        stat_max=summary(statistic.max(axis=(0, 1))),
        stat_mean=summary(statistic.mean(axis=(0, 1))),
        stretch_low_value=preview.attributes["stretch_low_value"],
        stretch_high_value=preview.attributes["stretch_high_value"],
        stretch_domain=preview.attributes["stretch_domain"],
        frames_analyzed=len(data.attributes["presentation_indices"]),
        destripe=options.get("destripe", False),
    )


def test_std_reveals_hidden_quiet_region(noise_video: Path) -> None:
    """复现"噪点藏图"场景：低时间方差区域在 std 图中显著偏暗。"""
    result = temporal_reduce(noise_video, op="std")
    x, y, w, h = NOISE_QUIET_RECT
    image = result.image.astype(np.float64)
    inside = image[y : y + h, x : x + w].mean()
    outside_mask = np.ones((NOISE_SIZE, NOISE_SIZE), dtype=bool)
    outside_mask[y : y + h, x : x + w] = False
    outside = image[outside_mask].mean()
    assert inside < outside - 60, f"藏图区域未显形：内 {inside} vs 外 {outside}"


def test_destripe_keeps_hidden_region_visible(noise_video: Path) -> None:
    result = temporal_reduce(noise_video, op="std", destripe=True)
    assert result.destripe is True
    x, y, w, h = NOISE_QUIET_RECT
    image = result.image.astype(np.float64)
    inside = image[y : y + h, x : x + w].mean()
    outside_mask = np.ones((NOISE_SIZE, NOISE_SIZE), dtype=bool)
    outside_mask[y : y + h, x : x + w] = False
    outside = image[outside_mask].mean()
    # 去条纹只影响可视化，藏图区域仍应显著偏暗；统计摘要保持原始数值
    assert inside < outside - 40
    assert result.stat_mean[0] > 0
    # 去条纹后是零中心残差空间：低端为负（藏图区低于趋势），高端为正
    assert result.stretch_domain == "detrended_residual"
    assert result.stretch_low_value < 0 < result.stretch_high_value
    # 无 destripe 时端点在原始统计空间
    raw = temporal_reduce(noise_video, op="std")
    assert raw.stretch_domain == "raw"
    assert raw.stretch_low_value > 0
    # smooth 也改变分布，domain 必须如实标注；与 destripe 可组合
    smoothed = temporal_reduce(noise_video, op="std", smooth=5)
    assert smoothed.stretch_domain == "smoothed"
    both = temporal_reduce(noise_video, op="std", destripe=True, smooth=5)
    assert both.stretch_domain == "detrended_residual+smoothed"


def test_mean_and_min_on_fixed_green_pixel(test_video: Path) -> None:
    gx, gy = GREEN_POS
    rect = (gx, gy, 1, 1)
    mean = temporal_reduce(test_video, op="mean", rect=rect)
    # 绿像素 G 恒为 255（闪白帧也是 255）；R/B 仅闪白帧为 255
    assert mean.stat_mean[1] == 255.0
    assert abs(mean.stat_mean[0] - 255 / FRAME_COUNT) < 0.01
    minimum = temporal_reduce(test_video, op="min", rect=rect)
    assert minimum.stat_min == [0.0, 255.0, 0.0]
    maximum = temporal_reduce(test_video, op="max", rect=rect)
    assert maximum.stat_max == [255.0, 255.0, 255.0]


def test_median_ignores_single_flash(test_video: Path) -> None:
    gx, gy = GREEN_POS
    result = temporal_reduce(test_video, op="median", rect=(gx, gy, 1, 1))
    # 30 帧中仅 1 帧闪白，中位数应还原绿色
    assert result.stat_mean == [0.0, 255.0, 0.0]


def test_diff_counts_pairs(test_video: Path) -> None:
    result = temporal_reduce(test_video, op="diff")
    assert result.frames_analyzed == FRAME_COUNT
    # 运动能量图非全零（红点移动 + 闪白）
    assert max(result.stat_max) > 0


def test_median_memory_guard(test_video: Path) -> None:
    # median 需要持有全部采样帧；超出请求的内存预算时在计算前拒绝
    with pytest.raises(MaterializationLimitExceededError, match="内存预算"):
        temporal_reduce(test_video, op="median", max_memory_bytes=4096)


def test_invalid_op_and_percentiles(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        temporal_reduce(test_video, op="avg")  # type: ignore[arg-type]
    with pytest.raises(InvalidRangeError):
        temporal_reduce(test_video, op="std", p_low=60, p_high=40)


def test_std_needs_two_frames(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        temporal_reduce(test_video, op="std", start_frame=3, end_frame=3)


def test_cli_reduce_export(noise_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "统计图.png"
    data = run_json(
        "reduce", noise_video, "--op", "std", "--output", out, "--json"
    )["data"]
    assert data["op"] == "std"
    assert data["output_path"] == str(out)
    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == (NOISE_SIZE, NOISE_SIZE)


def test_cli_reduce_rejects_bad_op(test_video: Path) -> None:
    code, data = run_json_error(
        "reduce", test_video, "--op", "sum", "--json"
    )
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"
