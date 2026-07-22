"""矩形区域相关模型。"""

from __future__ import annotations

from pydantic import BaseModel

from pixelprobe.models.pixel import HSV, Lab


class Rect(BaseModel):
    """矩形区域，x/y 为左上角，含 width 列、height 行。"""

    x: int
    y: int
    width: int
    height: int


class RGBStats(BaseModel):
    """逐通道统计值（浮点，保留精度由生成端控制）。"""

    r: float
    g: float
    b: float


class RegionStatistics(BaseModel):
    """矩形区域统计结果。"""

    rect: Rect
    pixel_count: int
    mean_rgb: RGBStats
    median_rgb: RGBStats
    min_rgb: RGBStats
    max_rgb: RGBStats
    std_rgb: RGBStats
    mean_hsv: HSV
    mean_lab: Lab
    mean_luminance: float
    std_luminance: float
