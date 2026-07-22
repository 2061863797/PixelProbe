"""媒体基本信息模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class MediaInfo(BaseModel):
    """图片或视频的元数据。

    frame_count_estimated 为 True 时表示总帧数由 duration × fps 估算而来
    （容器元数据缺失时的兜底），并非精确计数。
    """

    path: str
    media_type: Literal["image", "video"]
    width: int
    height: int
    channels: int
    fps: float | None = None
    frame_count: int | None = None
    frame_count_estimated: bool = False
    duration_seconds: float | None = None
    codec: str | None = None
    pixel_format: str | None = None
    color_mode: str | None = None
    is_vfr: bool | None = None
    time_base: str | None = None
    file_size_bytes: int
