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

from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import CoordinateOutOfRangeError, DecodeError

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
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        width, height = info.width, info.height

        if slice_type == "xt":
            if fixed < 0 or fixed >= height:
                raise CoordinateOutOfRangeError(
                    f"y={fixed} 超出有效范围 0～{height - 1}"
                )
        else:
            if fixed < 0 or fixed >= width:
                raise CoordinateOutOfRangeError(
                    f"x={fixed} 超出有效范围 0～{width - 1}"
                )

        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every
        )
        total = frame_range.count
        rows: list[np.ndarray] = []
        frames: list[int] = []
        times: list[float] = []
        for idx, t, arr in reader.iter_frames(
            frame_range.start, frame_range.end, frame_range.sample_every
        ):
            if slice_type == "xt":
                rows.append(arr[fixed, :, :].copy())
            else:
                rows.append(arr[:, fixed, :].copy())
            frames.append(idx)
            times.append(t)
            if progress is not None:
                progress(len(rows), total)

        if not rows:
            raise DecodeError("指定范围内没有解码出任何帧")
        return SpacetimeResult(
            array=np.stack(rows, axis=0),
            slice_type=slice_type,
            fixed_coordinate=fixed,
            frames=frames,
            times=times,
            frame_range=frame_range,
            width=width,
            height=height,
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
