"""给 AI 查看的图片加坐标刻度与 cell 网格。

刻度标注的是**原始存储坐标**（或帧号），不是显示像素：AI 可以直接读出位置并写进地址。
图片会按 max_side 缩小（面积平均）或在过小时按整数倍最近邻放大，缩放比例随结果返回：

    原始坐标 = offset + (显示坐标 - 边距) / scale

cell 网格与地址中的 cell(C4) 使用同一划分规则（floor(i × 尺寸 / 格数)），
图上标出的格子名可以原样写进地址。
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pixelprobe.address.model import DEFAULT_CELL_COLUMNS, DEFAULT_CELL_ROWS, column_label

MIN_LONG_SIDE = 256
MAX_UPSCALE = 16
_MARGIN_LEFT = 52
_MARGIN_TOP = 18
_MARGIN_BOTTOM = 22
_BACKGROUND = (24, 24, 24)
_FOREGROUND = (230, 230, 230)
_GRID = (255, 220, 0)


@dataclass(frozen=True)
class Axis:
    """一条坐标轴：length 为原始长度；label(i) 给出第 i 个原始位置的刻度文字。"""

    name: str
    length: int
    offset: int = 0
    label: Callable[[int], str] | None = None

    def text(self, index: int) -> str:
        return self.label(index) if self.label is not None else str(self.offset + index)


@dataclass(frozen=True)
class AnnotatedImage:
    png: bytes
    width: int
    height: int
    meta: dict


def _nice_step(span: int, target: int = 8) -> int:
    raw = max(1.0, span / target)
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if factor * power >= raw:
            return int(factor * power)
    return int(10 * power)


def _scale_for(height: int, width: int, max_side: int) -> float:
    long_side = max(height, width)
    if long_side > max_side:
        return max_side / long_side
    if long_side < MIN_LONG_SIDE:
        # 小图按整数倍放大到约 MIN_LONG_SIDE，但放大后也不能超过调用方给的 max_side
        factor = min(MAX_UPSCALE, MIN_LONG_SIDE // long_side, max_side // long_side)
        return float(max(1, factor))
    return 1.0


def _resize(image: np.ndarray, scale: float) -> np.ndarray:
    if scale == 1.0:
        return image
    height, width = image.shape[:2]
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    method = Image.Resampling.NEAREST if scale > 1 else Image.Resampling.BOX
    return np.asarray(Image.fromarray(image).resize(size, method))


def _cell_edges(length: int, count: int) -> list[int]:
    return [index * length // count for index in range(count + 1)]


def annotate(
    image: np.ndarray,
    *,
    x_axis: Axis | None = None,
    y_axis: Axis | None = None,
    grid: bool = False,
    title: str | None = None,
    max_side: int = 1024,
    max_bytes: int | None = None,
) -> AnnotatedImage:
    """返回带刻度（可选网格）的 PNG；超过 max_bytes 时逐步缩小，仍超出则报错。"""
    array = np.ascontiguousarray(image)
    if array.ndim == 2:
        array = np.stack([array] * 3, axis=-1)
    if array.shape[2] == 4:
        array = array[..., :3]
    side = max_side
    while True:
        result = _render(array, x_axis, y_axis, grid, title, side)
        if max_bytes is None or ((len(result.png) + 2) // 3) * 4 <= max_bytes or side <= 128:
            break
        side = max(128, side // 2)
    if max_bytes is not None and ((len(result.png) + 2) // 3) * 4 > max_bytes:
        raise ValueError("图片缩小到 128 像素后仍超过 MCP 图片载荷上限；请用 rect(...) 缩小范围")
    return result


def _render(
    array: np.ndarray, x_axis: Axis | None, y_axis: Axis | None, grid: bool,
    title: str | None, max_side: int,
) -> AnnotatedImage:
    source_height, source_width = array.shape[:2]
    scale = _scale_for(source_height, source_width, max_side)
    shown = _resize(array, scale)
    height, width = shown.shape[:2]
    scale_x = width / source_width
    scale_y = height / source_height
    left = _MARGIN_LEFT if y_axis is not None else 0
    top = _MARGIN_TOP if (title or x_axis is not None) else 0
    bottom = _MARGIN_BOTTOM if x_axis is not None else 0
    canvas = Image.new("RGB", (left + width + 4, top + height + bottom), _BACKGROUND)
    canvas.paste(Image.fromarray(shown), (left, top))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    if title:
        draw.text((left, 2), title, fill=_FOREGROUND, font=font)
    if x_axis is not None:
        step = _nice_step(source_width)
        for index in range(0, source_width, step):
            x = left + int((index + 0.5) * scale_x)
            draw.line([(x, top + height), (x, top + height + 4)], fill=_FOREGROUND)
            draw.text((x + 2, top + height + 5), x_axis.text(index), fill=_FOREGROUND, font=font)
    if y_axis is not None:
        step = _nice_step(source_height)
        for index in range(0, source_height, step):
            y = top + int((index + 0.5) * scale_y)
            draw.line([(left - 4, y), (left, y)], fill=_FOREGROUND)
            draw.text((2, max(top, y - 5)), y_axis.text(index)[:8], fill=_FOREGROUND, font=font)
    cells = None
    if grid and source_width >= DEFAULT_CELL_COLUMNS and source_height >= DEFAULT_CELL_ROWS:
        xs = _cell_edges(source_width, DEFAULT_CELL_COLUMNS)
        ys = _cell_edges(source_height, DEFAULT_CELL_ROWS)
        for edge in xs[1:-1]:
            x = left + int(edge * scale_x)
            draw.line([(x, top), (x, top + height - 1)], fill=_GRID)
        for edge in ys[1:-1]:
            y = top + int(edge * scale_y)
            draw.line([(left, y), (left + width - 1, y)], fill=_GRID)
        for column in range(DEFAULT_CELL_COLUMNS):
            for row in range(DEFAULT_CELL_ROWS):
                name = f"{column_label(column)}{row + 1}"
                position = (left + int(xs[column] * scale_x) + 2, top + int(ys[row] * scale_y) + 1)
                box = draw.textbbox(position, name, font=font)
                draw.rectangle(box, fill=(0, 0, 0))
                draw.text(position, name, fill=_GRID, font=font)
        cells = {"columns": DEFAULT_CELL_COLUMNS, "rows": DEFAULT_CELL_ROWS,
                 "address_example": "cell(C4)"}
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    meta = {
        "source_width": source_width,
        "source_height": source_height,
        "scale_x": round(scale_x, 6),
        "scale_y": round(scale_y, 6),
        "resampling": "none" if scale == 1.0 else ("nearest" if scale > 1 else "box_average"),
        "margin_left": left,
        "margin_top": top,
        "x_axis": None if x_axis is None else {"name": x_axis.name, "offset": x_axis.offset},
        "y_axis": None if y_axis is None else {"name": y_axis.name, "offset": y_axis.offset},
        "cell_grid": cells,
        "mapping": "原始坐标 = offset + (显示坐标 - 边距) / scale；缩小后的图只用于定位，数值请用测量工具读取",
    }
    return AnnotatedImage(png=buffer.getvalue(), width=canvas.width, height=canvas.height, meta=meta)


def frame_axis(frames: Sequence[int], name: str = "frame") -> Axis:
    """把行/列序号标成帧号（用于时空切片、时间线的时间轴）。"""
    values = list(frames)
    return Axis(name=name, length=len(values), label=lambda i: f"f{values[i]}")
