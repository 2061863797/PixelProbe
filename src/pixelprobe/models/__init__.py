"""Pydantic 数据模型。"""

from pixelprobe.models.errors import (
    CoordinateOutOfRangeError,
    DecodeError,
    FrameOutOfRangeError,
    InvalidRangeError,
    MediaNotFoundError,
    OutputWriteError,
    PixelProbeError,
    TimeOutOfRangeError,
    UnsupportedMediaError,
)
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.models.pixel import HSV, RGB, PixelCoordinate, PixelSample
from pixelprobe.models.region import Rect, RegionStatistics, RGBStats
from pixelprobe.models.spacetime import SpacetimeMetadata
from pixelprobe.models.timeline import TimelineMetadata

__all__ = [
    "PixelProbeError",
    "MediaNotFoundError",
    "UnsupportedMediaError",
    "DecodeError",
    "CoordinateOutOfRangeError",
    "FrameOutOfRangeError",
    "TimeOutOfRangeError",
    "InvalidRangeError",
    "OutputWriteError",
    "MediaInfo",
    "RGB",
    "HSV",
    "PixelCoordinate",
    "PixelSample",
    "Rect",
    "RGBStats",
    "RegionStatistics",
    "TimelineMetadata",
    "SpacetimeMetadata",
]
