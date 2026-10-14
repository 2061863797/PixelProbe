"""CLI 使用的轻量分析门面，不负责参数解析与终端输出。

切片、时间线、时间合成、频谱和光流统一经 `pixelprobe.generate` 走引擎，
请求由 `pixelprobe.request_builders` 构造，不在这里另设实现。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from pixelprobe.core.change_detector import (
    detect_changes,
    segment_events,
    top_changes,
)
from pixelprobe.core.contact_sheet import sample_frames
from pixelprobe.core.frame_compare import compare_frames
from pixelprobe.core.media_reader import get_media_info, load_frame, load_native_image
from pixelprobe.core.media_scanner import scan_media
from pixelprobe.core.pixel_inspector import inspect_native_pixels, inspect_pixels
from pixelprobe.core.region_analyzer import analyze_region
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.utils.coordinates import validate_rect

__all__ = [
    "MediaInfo",
    "analyze_region",
    "compare_frames",
    "detect_changes",
    "get_frame",
    "get_media_info",
    "inspect_native_pixels",
    "inspect_pixels",
    "load_frame",
    "load_native_image",
    "sample_frames",
    "scan_media",
    "segment_events",
    "top_changes",
]


def get_frame(
    path: Path,
    frame: int | None = None,
    time: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[np.ndarray, int | None, float | None, MediaInfo]:
    """取指定帧（可裁剪），返回 (RGB 数组, 帧号, 时间秒, 媒体信息)。"""
    arr, idx, t, info = load_frame(path, frame=frame, time=time)
    if crop is not None:
        x, y, w, h = crop
        validate_rect(x, y, w, h, info.width, info.height)
        arr = arr[y : y + h, x : x + w, :].copy()
    return arr, idx, t, info
