"""媒体类型识别与统一读取入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np

from pixelprobe.core.image_reader import ImageReader
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import InvalidRangeError, UnsupportedMediaError
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.utils.timecode import round_seconds
from pixelprobe.utils.validation import ensure_file_exists

_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff",
}
_VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg",
    ".wmv", ".flv", ".ts",
}


def detect_media_type(path: Path) -> Literal["image", "video"]:
    """根据扩展名判断媒体类型；未知扩展名依次尝试图片、视频解码。"""
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix in _VIDEO_EXTS:
        return "video"
    # 未知扩展名：先按图片试，再按视频试
    try:
        with ImageReader() as reader:
            reader.open(path)
        return "image"
    except UnsupportedMediaError:
        pass
    try:
        with VideoReader() as reader:
            reader.open(path)
        return "video"
    except UnsupportedMediaError:
        pass
    raise UnsupportedMediaError(
        f"无法识别的媒体格式：{path}",
        hint="支持 PNG/JPEG 等图片和 MP4/MKV 等视频",
    )


def get_media_info(path: Path) -> MediaInfo:
    """读取图片或视频的基本信息。"""
    path = ensure_file_exists(Path(path))
    if detect_media_type(path) == "image":
        with ImageReader() as reader:
            reader.open(path)
            return reader.get_info()
    with VideoReader() as reader:
        reader.open(path)
        return reader.get_info()


def load_frame(
    path: Path,
    frame: int | None = None,
    time: float | None = None,
) -> tuple[np.ndarray, int | None, float | None, MediaInfo]:
    """统一取帧入口。

    - 图片：不允许指定 frame/time，返回 (数组, None, None, info)；
    - 视频：frame 与 time 二选一（都不给默认第 0 帧），
      返回 (数组, 帧号, 时间秒, info)。
    """
    path = ensure_file_exists(Path(path))
    if frame is not None and time is not None:
        raise InvalidRangeError("--frame 与 --time 不能同时使用")

    if detect_media_type(path) == "image":
        if frame is not None or time is not None:
            raise InvalidRangeError(
                "图片不支持 --frame / --time 参数",
                hint="这些参数仅对视频有效",
            )
        with ImageReader() as reader:
            reader.open(path)
            return reader.get_frame(), None, None, reader.get_info()

    with VideoReader() as reader:
        reader.open(path)
        info = reader.get_info()
        if time is not None:
            reader.validate_time(time)
            idx, t, arr = reader.get_frame_by_time(time)
        else:
            idx = frame if frame is not None else 0
            t, arr = reader.get_frame_by_index(idx)
        return arr, idx, round_seconds(t), info
