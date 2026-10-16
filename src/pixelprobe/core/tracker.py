"""点跟踪与跟随切片：逐帧块匹配，确定性、整数像素精度。

算法：在上一帧的跟踪位置取 (2p+1)×(2p+1) 亮度块作模板，在当前帧以上一位置为中心、
±search 像素的窗口内找平均绝对差（MAD，0～255）最小的位置；同分时取位移最小者。
模板每帧更新（跟随外观缓慢变化），代价是误差可能逐帧累积。最小 MAD 超过 lost_threshold
即判定跟丢并停止，不再猜测后续位置。

跟随切片：每帧在跟踪点所在行取以跟踪点为中心、宽 strip_width 的一段像素（越界处填 0），
自上而下按时间排列——被跟踪物体始终在中线上，能看出它自身外观随时间的变化。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from pixelprobe.core.color import luminance_array
from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.utils.coordinates import validate_point


@dataclass
class TrackResult:
    frames: list[int]
    times: list[float]
    points: list[tuple[int, int]]
    costs: list[float | None]
    lost_at: int | None
    first_frame: np.ndarray
    strip: np.ndarray
    parameters: dict = field(default_factory=dict)


def _luma(arr: np.ndarray) -> np.ndarray:
    return luminance_array(arr).astype(np.float32)


def _strip_row(arr: np.ndarray, x: int, y: int, width: int) -> np.ndarray:
    half = width // 2
    row = np.zeros((width, 3), dtype=np.uint8)
    left = x - half
    lo, hi = max(0, left), min(arr.shape[1], left + width)
    if hi > lo:
        row[lo - left: hi - left] = arr[y, lo:hi]
    return row


def _match(
    template: np.ndarray, luma: np.ndarray, x: int, y: int, patch: int, search: int,
) -> tuple[int, int, float]:
    height, width = luma.shape
    # 候选中心须使模板完整落在画面内
    x0, x1 = max(patch, x - search), min(width - patch - 1, x + search)
    y0, y1 = max(patch, y - search), min(height - patch - 1, y + search)
    region = luma[y0 - patch: y1 + patch + 1, x0 - patch: x1 + patch + 1]
    windows = sliding_window_view(region, template.shape)
    cost = np.abs(windows - template).mean(axis=(2, 3))
    best = float(cost.min())
    ys, xs = np.nonzero(cost == best)
    candidates = [(abs(y0 + dy - y) + abs(x0 + dx - x), y0 + dy, x0 + dx) for dy, dx in zip(ys, xs)]
    _, best_y, best_x = min(candidates)
    return int(best_x), int(best_y), best


def track_point(
    path: Path,
    frame_range: FrameRange,
    start: tuple[int, int],
    *,
    patch: int = 8,
    search: int = 16,
    lost_threshold: float = 40.0,
    strip_width: int = 64,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> TrackResult:
    if frame_range.sample_every != 1:
        raise InvalidRangeError("跟踪需要逐帧进行，地址中不能写 step=")
    frames: list[int] = []
    times: list[float] = []
    points: list[tuple[int, int]] = []
    costs: list[float | None] = []
    rows: list[np.ndarray] = []
    lost_at = None
    first = None
    template = None
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        validate_point(*start, info.width, info.height)
        if info.width < 2 * patch + 1 or info.height < 2 * patch + 1:
            raise InvalidRangeError(f"画面小于跟踪块（{2 * patch + 1} 像素）")
        x = min(max(start[0], patch), info.width - patch - 1)
        y = min(max(start[1], patch), info.height - patch - 1)
        for index, timestamp, arr in reader.iter_frames(frame_range.start, frame_range.end, 1):
            if cancel_check is not None:
                cancel_check()
            luma = _luma(arr)
            if template is None:
                first, cost = arr, None
            else:
                x, y, cost = _match(template, luma, x, y, patch, search)
                if cost > lost_threshold:
                    lost_at = index
                    break
            template = luma[y - patch: y + patch + 1, x - patch: x + patch + 1].copy()
            frames.append(index)
            times.append(round(float(timestamp), 6))
            points.append((x, y))
            costs.append(None if cost is None else round(cost, 4))
            rows.append(_strip_row(arr, x, y, strip_width))
            if progress is not None:
                progress(len(frames), frame_range.count)
    if first is None:
        raise DecodeError("指定范围内没有解码出任何帧")
    return TrackResult(
        frames=frames, times=times, points=points, costs=costs, lost_at=lost_at,
        first_frame=first, strip=np.stack(rows),
        parameters={"patch": patch, "search": search, "lost_threshold": lost_threshold,
                    "strip_width": strip_width, "start_clamped": points[0] != tuple(start)},
    )


def draw_path(image: np.ndarray, points: list[tuple[int, int]]) -> np.ndarray:
    """在首帧上画出轨迹（黄线、起点绿、终点红）。"""
    from PIL import Image, ImageDraw
    canvas = Image.fromarray(image).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    if len(points) > 1:
        draw.line(points, fill=(255, 220, 0), width=1)
    for (px, py), color in ((points[0], (0, 255, 0)), (points[-1], (255, 0, 0))):
        draw.rectangle((px - 1, py - 1, px + 1, py + 1), outline=color)
    return np.asarray(canvas)
