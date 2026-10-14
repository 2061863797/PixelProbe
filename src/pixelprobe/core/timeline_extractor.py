"""像素时间线的选点、排序与结果结构。

采样计算统一经引擎完成（见 `pixelprobe.request_builders.timeline_request`），
无论选择多少个点，视频只解码一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.domain.tensor import TensorField
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.models.pixel import PixelCoordinate
from pixelprobe.utils.coordinates import (
    grid_points,
    pixel_id_from_xy,
    validate_point,
    validate_rect,
    xy_from_pixel_id,
)

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
        raise InvalidRangeError("point(...)/pid(...) 与 grid(...) 不能同时使用")
    if grid is None and (step is not None or block_size is not None):
        raise InvalidRangeError("grid 的 step 与 --block-size 必须与 grid(...) 搭配使用")

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
            '未指定任何采样点，请在 --at 中写 point(x,y)、pid(n) 或 grid(x,y,w,h step=n)'
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
