"""像素时间线提取。

核心约束：无论选择多少个点，视频只解码一次；
每帧用 NumPy 花式索引一次性取出所有点。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from pixelprobe.compat.legacy_results import timeline_matrix
from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.domain.tensor import TensorField
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.operators.sampling import (
    SamplingPlan,
    execute_sampling,
)
from pixelprobe.models.pixel import PixelCoordinate
from pixelprobe.utils.coordinates import (
    grid_points,
    pixel_id_from_xy,
    validate_point,
    validate_rect,
    xy_from_pixel_id,
)

ProgressCallback = Callable[[int, int], None]

SortMode = Literal["selection", "pixel-id", "yx", "xy"]


@dataclass
class TimelineResult:
    """时间线提取结果。matrix 形状 [K, T, 3]，uint8 RGB。"""

    matrix: np.ndarray
    points: list[PixelCoordinate]
    frames: list[int]
    times: list[float]
    frame_range: FrameRange
    sample_type: Literal["point", "block_mean"]
    block_size: int | None
    sort: SortMode
    width: int
    height: int
    tensor: TensorField


def build_points(
    width: int,
    height: int,
    points: list[tuple[int, int]] | None = None,
    pixel_ids: list[int] | None = None,
    grid: tuple[int, int, int, int] | None = None,
    step: int | None = None,
    block_size: int | None = None,
) -> list[tuple[int, int]]:
    """把 CLI 的选点参数统一为坐标列表（选择顺序）。"""
    explicit = bool(points) or bool(pixel_ids)
    if explicit and grid is not None:
        raise InvalidRangeError(
            "--point/--pixel-id 与 --grid 不能同时使用"
        )
    if grid is None and (step is not None or block_size is not None):
        raise InvalidRangeError("--step / --block-size 必须与 --grid 搭配使用")

    result: list[tuple[int, int]] = []
    if explicit:
        for x, y in points or []:
            validate_point(x, y, width, height)
            result.append((x, y))
        for pid in pixel_ids or []:
            result.append(xy_from_pixel_id(pid, width, height))
    elif grid is not None:
        validate_rect(*grid, width, height)
        if block_size is not None and block_size < 1:
            raise InvalidRangeError(f"--block-size {block_size} 无效，必须 >= 1")
        spacing = step if step is not None else (block_size or 1)
        result = grid_points(grid, spacing)
    if not result:
        raise InvalidRangeError(
            "未指定任何采样点，请使用 --point / --pixel-id / --grid"
        )
    return result


def sort_points(
    pts: list[tuple[int, int]], sort: SortMode, width: int
) -> list[tuple[int, int]]:
    """按指定方式排序采样点。"""
    if sort == "selection":
        return list(pts)
    if sort == "pixel-id":
        return sorted(pts, key=lambda p: pixel_id_from_xy(p[0], p[1], width))
    if sort == "yx":
        return sorted(pts, key=lambda p: (p[1], p[0]))
    if sort == "xy":
        return sorted(pts, key=lambda p: (p[0], p[1]))
    raise InvalidRangeError(f"未知排序方式：{sort}")


def extract_timelines(
    path: Path,
    points: list[tuple[int, int]] | None = None,
    pixel_ids: list[int] | None = None,
    grid: tuple[int, int, int, int] | None = None,
    step: int | None = None,
    block_size: int | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    sort: SortMode = "selection",
    progress: ProgressCallback | None = None,
) -> TimelineResult:
    """提取多像素时间线，返回 [K, T, 3] 矩阵。视频只解码一遍。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        width, height = info.width, info.height

        pts = build_points(
            width, height,
            points=points, pixel_ids=pixel_ids,
            grid=grid, step=step, block_size=block_size,
        )
        pts = sort_points(pts, sort, width)
        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every
        )

        plan = SamplingPlan(
            kind="points_t",
            frame_range=frame_range,
            width=width,
            height=height,
            points=tuple((float(x), float(y)) for x, y in pts),
            block_size=block_size,
        )
        output = execute_sampling(reader, plan, progress)
        matrix = timeline_matrix(output.tensor)
        if block_size is not None:
            # 旧 CLI 历史上把块均值四舍五入为 uint8；新 Tensor 保留 float64。
            matrix = np.round(matrix).astype(np.uint8)

        coords = [
            PixelCoordinate(x=x, y=y, pixel_id=pixel_id_from_xy(x, y, width))
            for x, y in pts
        ]
        return TimelineResult(
            matrix=matrix,
            points=coords,
            frames=list(output.frames),
            times=list(output.times),
            frame_range=frame_range,
            sample_type="point" if block_size is None else "block_mean",
            block_size=block_size,
            sort=sort,
            width=width,
            height=height,
            tensor=output.tensor,
        )
