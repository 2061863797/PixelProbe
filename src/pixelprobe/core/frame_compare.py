"""任意两帧比较：差异热力图 + 变化区域统计。

回答"这两帧之间具体哪里变了、变了多少"：detect_changes 找到候选帧后，
用本模块比较峰值前后帧，把变化定位到具体区域（bbox）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.output.plot import apply_colormap
from pixelprobe.utils.coordinates import validate_rect

CompareColormap = Literal["gray", "fire"]


@dataclass
class CompareResult:
    """两帧比较结果。

    diff_image 是逐像素差异热力图（按最大差拉伸到 0-255 后伪彩），
    数值判断以 mean_abs_diff / max_abs_diff / changed_* 为准。
    """

    frame_a: int
    frame_b: int
    time_a: float
    time_b: float
    diff_image: np.ndarray
    mean_abs_diff: float
    max_abs_diff: int
    changed_pixels: int
    changed_ratio: float
    bbox: tuple[int, int, int, int] | None
    threshold: int
    rect: tuple[int, int, int, int] | None
    width: int
    height: int


def _resolve_frame(
    reader: VideoReader, label: str,
    frame: int | None, time: float | None,
) -> tuple[int, float, np.ndarray]:
    if (frame is None) == (time is None):
        raise InvalidRangeError(
            f"帧 {label} 必须且只能指定 frame_{label} 或 time_{label} 之一"
        )
    if frame is not None:
        t, arr = reader.get_frame_by_index(frame)
        return frame, t, arr
    assert time is not None
    idx, t, arr = reader.get_frame_by_time(time)
    return idx, t, arr


def compare_frames(
    path: Path,
    frame_a: int | None = None,
    time_a: float | None = None,
    frame_b: int | None = None,
    time_b: float | None = None,
    rect: tuple[int, int, int, int] | None = None,
    threshold: int = 10,
    colormap: CompareColormap = "fire",
) -> CompareResult:
    """比较视频中任意两帧（各自用帧号或秒指定），返回差异图与区域统计。

    差异按每像素三通道绝对差的最大值衡量；超过 threshold 的像素计为
    "变化像素"，其外接矩形即 bbox（原始分辨率坐标；rect 模式下同样为
    原始分辨率坐标）。
    """
    if not (0 <= threshold <= 255):
        raise InvalidRangeError(f"threshold {threshold} 无效，必须在 0～255 内")
    if colormap not in ("gray", "fire"):
        raise InvalidRangeError(f"colormap {colormap!r} 无效，可选 gray/fire")

    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        width, height = info.width, info.height
        if rect is not None:
            validate_rect(*rect, width, height)
        idx_a, t_a, arr_a = _resolve_frame(reader, "a", frame_a, time_a)
        idx_b, t_b, arr_b = _resolve_frame(reader, "b", frame_b, time_b)

    x0 = y0 = 0
    if rect is not None:
        x0, y0, w, h = rect
        arr_a = arr_a[y0 : y0 + h, x0 : x0 + w, :]
        arr_b = arr_b[y0 : y0 + h, x0 : x0 + w, :]

    # uint8 直接相减会下溢；max-min 与绝对差等价
    diff = np.maximum(arr_a, arr_b) - np.minimum(arr_a, arr_b)
    diff_max = diff.max(axis=2)  # 每像素三通道最大差 [H,W]
    mask = diff_max > threshold

    changed_pixels = int(mask.sum())
    bbox: tuple[int, int, int, int] | None = None
    if changed_pixels > 0:
        ys, xs = np.nonzero(mask)
        bbox = (
            x0 + int(xs.min()),
            y0 + int(ys.min()),
            int(xs.max() - xs.min()) + 1,
            int(ys.max() - ys.min()) + 1,
        )

    peak = int(diff_max.max())
    if peak > 0:
        norm = (diff_max.astype(np.float64) * (255.0 / peak) + 0.5).astype(np.uint8)
    else:
        norm = diff_max  # 全零
    return CompareResult(
        frame_a=idx_a,
        frame_b=idx_b,
        time_a=t_a,
        time_b=t_b,
        diff_image=apply_colormap(norm, colormap),
        mean_abs_diff=round(float(diff.mean()), 4),
        max_abs_diff=peak,
        changed_pixels=changed_pixels,
        changed_ratio=round(changed_pixels / mask.size, 6),
        bbox=bbox,
        threshold=threshold,
        rect=rect,
        width=width,
        height=height,
    )
