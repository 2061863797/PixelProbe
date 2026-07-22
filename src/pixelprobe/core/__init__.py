"""核心分析层。

不依赖 Typer / Rich，可被 CLI、MCP、GUI 和 Python API 直接复用。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from pixelprobe.core.change_detector import (
    ChangeRecord,
    ChangesResult,
    detect_changes,
    top_changes,
)
from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.image_reader import ImageReader
from pixelprobe.core.media_reader import (
    detect_media_type,
    get_media_info,
    load_frame,
)
from pixelprobe.core.pixel_inspector import inspect_pixels
from pixelprobe.core.region_analyzer import analyze_region
from pixelprobe.core.spacetime_slice import (
    SpacetimeResult,
    create_xt_slice,
    create_yt_slice,
)
from pixelprobe.core.timeline_extractor import (
    TimelineResult,
    extract_timelines,
)
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.utils.coordinates import validate_rect


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


__all__ = [
    "get_media_info",
    "get_frame",
    "load_frame",
    "detect_media_type",
    "inspect_pixels",
    "analyze_region",
    "extract_timelines",
    "create_xt_slice",
    "create_yt_slice",
    "detect_changes",
    "top_changes",
    "resolve_range",
    "FrameRange",
    "TimelineResult",
    "SpacetimeResult",
    "ChangesResult",
    "ChangeRecord",
    "ImageReader",
    "VideoReader",
    "MediaInfo",
]
