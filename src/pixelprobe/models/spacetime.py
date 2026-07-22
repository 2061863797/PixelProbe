"""时空切片（X–T / Y–T）相关模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SpacetimeMetadata(BaseModel):
    """xt / yt 命令输出的元数据。

    - xt：space_axis = "original_x"，fixed_coordinate 为扫描线 y；
    - yt：space_axis = "original_y"，fixed_coordinate 为扫描线 x。
    时间轴均为纵向（从上到下时间递增）。
    width / height 为最终输出图片尺寸（含放大）。
    """

    slice_type: Literal["xt", "yt"]
    fixed_coordinate: int
    start_frame: int
    end_frame: int
    frame_count: int
    sample_every: int
    space_axis: Literal["original_x", "original_y"]
    time_axis: Literal["vertical"]
    width: int
    height: int
    scale_space: int
    scale_t: int
    output_path: str | None = None
