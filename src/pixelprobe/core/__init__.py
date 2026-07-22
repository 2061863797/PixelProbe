"""核心分析层。

不依赖 Typer / Rich，可被 CLI、MCP、GUI 和 Python API 直接复用。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from pixelprobe.core.change_detector import (
    ChangeEvent,
    ChangeRecord,
    ChangesResult,
    detect_changes,
    segment_events,
    top_changes,
)
from pixelprobe.core.contact_sheet import (
    ContactSheetResult,
    compose_sheet,
    plan_sheet_frames,
    sample_frames,
)
from pixelprobe.core.frame_compare import CompareResult, compare_frames
from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.frequency import (
    SpatialSpectrumResult,
    TemporalSpectrumResult,
    spatial_spectrum,
    temporal_spectrum,
)
from pixelprobe.core.image_reader import ImageReader
from pixelprobe.core.media_reader import (
    detect_media_type,
    get_media_info,
    load_frame,
)
from pixelprobe.core.media_scanner import ScanResult, scan_media
from pixelprobe.core.optical_flow import FlowResult, compute_flow
from pixelprobe.core.pixel_inspector import inspect_pixels
from pixelprobe.core.region_analyzer import analyze_region
from pixelprobe.core.spacetime_slice import (
    SpacetimeResult,
    create_xt_slice,
    create_yt_slice,
)
from pixelprobe.core.temporal_reduce import (
    REDUCE_OPS,
    TemporalReduceResult,
    temporal_reduce,
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
    "segment_events",
    "temporal_reduce",
    "compare_frames",
    "sample_frames",
    "plan_sheet_frames",
    "compose_sheet",
    "resolve_range",
    "scan_media",
    "temporal_spectrum",
    "spatial_spectrum",
    "compute_flow",
    "REDUCE_OPS",
    "FrameRange",
    "TimelineResult",
    "SpacetimeResult",
    "ChangesResult",
    "ChangeRecord",
    "ChangeEvent",
    "TemporalReduceResult",
    "CompareResult",
    "ContactSheetResult",
    "ScanResult",
    "TemporalSpectrumResult",
    "SpatialSpectrumResult",
    "FlowResult",
    "ImageReader",
    "VideoReader",
    "MediaInfo",
]
