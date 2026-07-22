"""矩形区域统计分析。"""

from __future__ import annotations

import numpy as np

from pixelprobe.core.color import (
    luminance_array,
    rgb_to_hsv_array,
    rgb_to_lab_array,
)
from pixelprobe.models.pixel import HSV, Lab
from pixelprobe.models.region import Rect, RegionStatistics, RGBStats
from pixelprobe.utils.coordinates import validate_rect


def _channel_stats(values: np.ndarray) -> RGBStats:
    """把 [3] 形状的逐通道数值转成 RGBStats（保留 4 位小数）。"""
    return RGBStats(
        r=round(float(values[0]), 4),
        g=round(float(values[1]), 4),
        b=round(float(values[2]), 4),
    )


def analyze_region(
    frame_array: np.ndarray, rect: tuple[int, int, int, int]
) -> RegionStatistics:
    """计算矩形区域的 RGB / HSV / 亮度统计。

    frame_array 形状 [height, width, 3]，uint8 RGB。
    矩形必须完整落在画面内，否则抛 CoordinateOutOfRangeError。
    注意：mean_hsv 中的色相为算术平均（未做圆周平均），
    区域内色相跨越 0°/360° 时该值仅供参考。
    """
    height, width = frame_array.shape[:2]
    x, y, w, h = rect
    validate_rect(x, y, w, h, width, height)

    patch = frame_array[y : y + h, x : x + w, :].reshape(-1, 3)
    patch_f = patch.astype(np.float64)
    hsv = rgb_to_hsv_array(patch)
    lab = rgb_to_lab_array(patch)
    lum = luminance_array(patch)

    return RegionStatistics(
        rect=Rect(x=x, y=y, width=w, height=h),
        pixel_count=int(patch.shape[0]),
        mean_rgb=_channel_stats(patch_f.mean(axis=0)),
        median_rgb=_channel_stats(np.median(patch_f, axis=0)),
        min_rgb=_channel_stats(patch_f.min(axis=0)),
        max_rgb=_channel_stats(patch_f.max(axis=0)),
        std_rgb=_channel_stats(patch_f.std(axis=0)),
        mean_hsv=HSV(
            h=round(float(hsv[:, 0].mean()), 2),
            s=round(float(hsv[:, 1].mean()), 2),
            v=round(float(hsv[:, 2].mean()), 2),
        ),
        mean_lab=Lab(
            l=round(float(lab[:, 0].mean()), 2),
            a=round(float(lab[:, 1].mean()), 2),
            b=round(float(lab[:, 2].mean()), 2),
        ),
        mean_luminance=round(float(lum.mean()), 4),
        std_luminance=round(float(lum.std()), 4),
    )
