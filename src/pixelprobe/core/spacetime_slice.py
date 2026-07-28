"""X–T / Y–T 时空切片。

- X–T：固定 y，取每帧 frame[y, 0:width]，按时间从上到下堆叠，结果 [T, W, 3]；
- Y–T：固定 x，取每帧 frame[0:height, x]，每帧作为一行，结果 [T, H, 3]。
两者时间轴均为纵向（第 0 行是范围内最早的帧）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from pixelprobe.compat.legacy_results import spacetime_array
from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.domain.tensor import TensorField
from pixelprobe.operators.sampling import (
    sample_xt as sample_xt_tensor,
    sample_yt as sample_yt_tensor,
)

ProgressCallback = Callable[[int, int], None]


@dataclass
class SpacetimeResult:
    """时空切片结果。array 形状 [T, 空间长度, 3]，uint8 RGB。"""

    array: np.ndarray
    slice_type: Literal["xt", "yt"]
    fixed_coordinate: int
    frames: list[int]
    times: list[float]
    frame_range: FrameRange
    width: int
    height: int
    tensor: TensorField


def _create_slice(
    path: Path,
    slice_type: Literal["xt", "yt"],
    fixed: int,
    start_frame: int | None,
    end_frame: int | None,
    start: float | None,
    end: float | None,
    sample_every: int,
    progress: ProgressCallback | None,
) -> SpacetimeResult:
    sampler = sample_xt_tensor if slice_type == "xt" else sample_yt_tensor
    output = sampler(
        Path(path),
        fixed,
        start_frame=start_frame,
        end_frame=end_frame,
        start=start,
        end=end,
        sample_every=sample_every,
        progress=progress,
    )
    return SpacetimeResult(
        array=spacetime_array(output.tensor, "x" if slice_type == "xt" else "y"),
        slice_type=slice_type,
        fixed_coordinate=fixed,
        frames=list(output.frames),
        times=list(output.times),
        frame_range=output.plan.frame_range,
        width=output.plan.width,
        height=output.plan.height,
        tensor=output.tensor,
    )


def create_xt_slice(
    path: Path,
    y: int,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    progress: ProgressCallback | None = None,
) -> SpacetimeResult:
    """生成水平扫描线的 X–T 切片，结果 [T, W, 3]。"""
    return _create_slice(
        path, "xt", y, start_frame, end_frame, start, end, sample_every, progress
    )


def create_yt_slice(
    path: Path,
    x: int,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    progress: ProgressCallback | None = None,
) -> SpacetimeResult:
    """生成垂直扫描线的 Y–T 切片，结果 [T, H, 3]。"""
    return _create_slice(
        path, "yt", x, start_frame, end_frame, start, end, sample_every, progress
    )
