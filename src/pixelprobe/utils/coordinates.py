"""坐标解析、换算与校验工具。

约定：
- 原点在左上角，x 向右，y 向下；0 <= x < width，0 <= y < height；
- pixel_id = y * width + x；
- x = pixel_id % width；y = pixel_id // width。
"""

from __future__ import annotations

from pixelprobe.models.errors import CoordinateOutOfRangeError, InvalidRangeError


def parse_point(text: str) -> tuple[int, int]:
    """解析 "x,y" 形式的坐标字符串。格式错误抛 InvalidRangeError。"""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        raise InvalidRangeError(
            f"坐标格式错误：{text!r}，应为 x,y（例如 520,340）"
        )
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise InvalidRangeError(
            f"坐标格式错误：{text!r}，x 和 y 必须是整数"
        ) from exc


def parse_rect(text: str) -> tuple[int, int, int, int]:
    """解析 "x,y,width,height" 形式的矩形字符串。"""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise InvalidRangeError(
            f"矩形格式错误：{text!r}，应为 x,y,width,height（例如 400,200,300,300）"
        )
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError as exc:
        raise InvalidRangeError(
            f"矩形格式错误：{text!r}，四个分量必须是整数"
        ) from exc
    if w < 1 or h < 1:
        raise InvalidRangeError(
            f"矩形尺寸无效：width={w}, height={h}，两者必须 >= 1"
        )
    return x, y, w, h


def pixel_id_from_xy(x: int, y: int, width: int) -> int:
    """由坐标计算像素编号。"""
    return y * width + x


def xy_from_pixel_id(pixel_id: int, width: int, height: int) -> tuple[int, int]:
    """由像素编号还原坐标，越界抛 CoordinateOutOfRangeError。"""
    max_id = width * height - 1
    if pixel_id < 0 or pixel_id > max_id:
        raise CoordinateOutOfRangeError(
            f"pixel_id={pixel_id} 超出有效范围 0～{max_id}",
            hint=f"该媒体共 {width}×{height} 个像素",
        )
    return pixel_id % width, pixel_id // width


def validate_point(x: int, y: int, width: int, height: int) -> None:
    """校验坐标是否落在画面内，越界抛 CoordinateOutOfRangeError。"""
    if x < 0 or x >= width or y < 0 or y >= height:
        raise CoordinateOutOfRangeError(
            f"坐标 ({x},{y}) 超出画面范围。有效 x：0～{width - 1}，有效 y：0～{height - 1}",
            hint="坐标原点在左上角，x 向右，y 向下",
        )


def validate_rect(
    x: int, y: int, w: int, h: int, width: int, height: int
) -> None:
    """校验矩形是否完整落在画面内。"""
    if x < 0 or y < 0 or w < 1 or h < 1 or x + w > width or y + h > height:
        raise CoordinateOutOfRangeError(
            f"矩形 ({x},{y},{w},{h}) 超出画面范围。"
            f"要求 0 <= x，x+width <= {width}，0 <= y，y+height <= {height}",
            hint=f"画面尺寸为 {width}×{height}",
        )


def grid_points(
    rect: tuple[int, int, int, int], step: int
) -> list[tuple[int, int]]:
    """在矩形内按步长生成采样点，行优先（先左到右，再上到下）。

    采样点从矩形左上角开始，间隔 step，只包含落在矩形内的点。
    """
    if step < 1:
        raise InvalidRangeError(f"step={step} 无效，必须 >= 1")
    x0, y0, w, h = rect
    return [
        (x, y)
        for y in range(y0, y0 + h, step)
        for x in range(x0, x0 + w, step)
    ]
