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


def inspect_native_pixels(
    image_array: np.ndarray,
    points: list[tuple[int, int]],
    *,
    bands: tuple[str, ...],
    sample_semantics: str,
) -> list[dict[str, object]]:
    """读取图片原生样本值，不套用 RGB/HSV/Lab 派生计算。

    二维图片返回单一通道值；三维图片按 ``bands`` 的顺序返回各通道。调色板
    图片的值是调色板索引，语义由 ``sample_semantics`` 与调用方元数据共同说明。
    """
    if image_array.ndim not in {2, 3}:
        raise ValueError("原生图片样本必须是二维或三维数组")
    height, width = image_array.shape[:2]
    channel_count = 1 if image_array.ndim == 2 else image_array.shape[2]
    if len(bands) != channel_count:
        raise ValueError("原生图片通道描述与数组 shape 不一致")
    for x, y in points:
        validate_point(x, y, width, height)

    samples: list[dict[str, object]] = []
    for x, y in points:
        value = image_array[y, x]
        if image_array.ndim == 2:
            values = [value.item()]
        else:
            values = [item.item() for item in value]
        samples.append({
            "x": x,
            "y": y,
            "pixel_id": pixel_id_from_xy(x, y, width),
            "channels": list(bands),
            "values": values,
            "dtype": str(image_array.dtype),
            "sample_semantics": sample_semantics,
        })
    return samples
