"""像素相关模型。"""

from __future__ import annotations

from pydantic import BaseModel


class RGB(BaseModel):
    r: int
    g: int
    b: int


class HSV(BaseModel):
    """H 范围 0～360，S/V 范围 0～100。"""

    h: float
    s: float
    v: float


class Lab(BaseModel):
    """CIELAB（D65）。L 范围 0～100，a/b 约 ±128。"""

    l: float
    a: float
    b: float


class PixelCoordinate(BaseModel):
    """像素坐标。pixel_id = y * width + x。"""

    x: int
    y: int
    pixel_id: int


class PixelSample(BaseModel):
    """某帧（或图片）中单个像素的完整采样结果。"""

    x: int
    y: int
    pixel_id: int
    frame: int | None = None
    time_seconds: float | None = None
    time_ms: float | None = None
    rgb: RGB
    hex: str
    hsv: HSV
    lab: Lab
    luminance: float
    luminance_linear: float
