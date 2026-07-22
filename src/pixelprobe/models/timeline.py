"""时间线相关模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from pixelprobe.models.pixel import PixelCoordinate


class TimelineMetadata(BaseModel):
    """timeline 命令输出的元数据。

    raw_width / raw_height 是未放大的原始矩阵尺寸：
    横向为时间（T 帧），纵向为像素点（K 个）。
    """

    points: list[PixelCoordinate]
    start_frame: int
    end_frame: int
    frame_count: int
    sample_every: int
    orientation: Literal["horizontal", "vertical"]
    sort: Literal["selection", "pixel-id", "yx", "xy"]
    sample_type: Literal["point", "block_mean"]
    block_size: int | None = None
    raw_width: int
    raw_height: int
    scale: int
    output_path: str | None = None
    raw_output_path: str | None = None
    csv_path: str | None = None
