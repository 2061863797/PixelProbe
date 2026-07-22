"""像素查询：从单帧数组中读取一个或多个像素的完整信息。"""

from __future__ import annotations

import numpy as np

from pixelprobe.core.color import (
    luminance_array,
    luminance_linear_array,
    rgb_to_hex,
    rgb_to_hsv_array,
    rgb_to_lab_array,
)
from pixelprobe.models.pixel import HSV, RGB, Lab, PixelSample
from pixelprobe.utils.coordinates import pixel_id_from_xy, validate_point
from pixelprobe.utils.timecode import seconds_to_ms


def inspect_pixels(
    frame_array: np.ndarray,
    points: list[tuple[int, int]],
    frame: int | None = None,
    time_seconds: float | None = None,
) -> list[PixelSample]:
    """读取指定坐标处的像素信息。

    frame_array 形状 [height, width, 3]，uint8 RGB。
    坐标越界抛 CoordinateOutOfRangeError。
    """
    height, width = frame_array.shape[:2]
    for x, y in points:
        validate_point(x, y, width, height)

    xs = np.array([p[0] for p in points], dtype=np.intp)
    ys = np.array([p[1] for p in points], dtype=np.intp)
    rgbs = frame_array[ys, xs, :]  # [K, 3]
    hsvs = rgb_to_hsv_array(rgbs)
    labs = rgb_to_lab_array(rgbs)
    lums = luminance_array(rgbs)
    lums_linear = luminance_linear_array(rgbs)

    samples: list[PixelSample] = []
    for i, (x, y) in enumerate(points):
        r, g, b = (int(v) for v in rgbs[i])
        samples.append(
            PixelSample(
                x=x,
                y=y,
                pixel_id=pixel_id_from_xy(x, y, width),
                frame=frame,
                time_seconds=time_seconds,
                time_ms=(
                    seconds_to_ms(time_seconds)
                    if time_seconds is not None
                    else None
                ),
                rgb=RGB(r=r, g=g, b=b),
                hex=rgb_to_hex(r, g, b),
                hsv=HSV(
                    h=round(float(hsvs[i, 0]), 2),
                    s=round(float(hsvs[i, 1]), 2),
                    v=round(float(hsvs[i, 2]), 2),
                ),
                lab=Lab(
                    l=round(float(labs[i, 0]), 2),
                    a=round(float(labs[i, 1]), 2),
                    b=round(float(labs[i, 2]), 2),
                ),
                luminance=round(float(lums[i]), 2),
                luminance_linear=round(float(lums_linear[i]), 2),
            )
        )
    return samples
