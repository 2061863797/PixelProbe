"""统一地址的数据结构：一个地址 = 时间选择 + 采样步长 + 若干空间形状。

坐标原点在左上角，x 向右、y 向下，单位为存储像素；帧号从 0 开始；
帧范围与时间范围都是闭区间，时间按"不晚于目标时间的最后一帧"对应到帧。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Union

TimeKind = Literal["frame", "time"]
TimeMode = Literal["single", "range", "list", "all"]


def _number(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True, slots=True)
class TimeSpec:
    """时间选择。kind=frame 时 values 为帧号，kind=time 时为秒。

    - single：values=(v,)
    - range：values=(start|None, end|None)，None 表示开放端
    - list：values=(v1, v2, ...)，至少两个
    - all：values=()
    """

    kind: TimeKind
    mode: TimeMode
    values: tuple[float | int | None, ...] = ()

    def __str__(self) -> str:
        prefix = "f" if self.kind == "frame" else "t"
        if self.mode == "all":
            return f"{prefix}=all"

        def fmt(value: float | int | None) -> str:
            if value is None:
                return ""
            return _number(value) if self.kind == "frame" else f"{_number(value)}s"

        if self.mode == "range":
            return f"{prefix}={fmt(self.values[0])}..{fmt(self.values[1])}"
        return f"{prefix}=" + ",".join(fmt(value) for value in self.values)


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: int

    def __str__(self) -> str:
        return f"point({self.x},{self.y})"


@dataclass(frozen=True, slots=True)
class PixelId:
    pixel_id: int

    def __str__(self) -> str:
        return f"pid({self.pixel_id})"


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def __str__(self) -> str:
        return f"rect({self.x},{self.y},{self.width},{self.height})"


@dataclass(frozen=True, slots=True)
class Grid:
    """矩形内的规则采样格点；step 缺省时由命令决定。"""

    x: int
    y: int
    width: int
    height: int
    step: int | None = None

    def __str__(self) -> str:
        step = "" if self.step is None else f" step={self.step}"
        return f"grid({self.x},{self.y},{self.width},{self.height}{step})"


@dataclass(frozen=True, slots=True)
class Row:
    """一整行像素，用于 X-T 切片。"""

    y: int

    def __str__(self) -> str:
        return f"row({self.y})"


@dataclass(frozen=True, slots=True)
class Col:
    """一整列像素，用于 Y-T 切片。"""

    x: int

    def __str__(self) -> str:
        return f"col({self.x})"


DEFAULT_CELL_COLUMNS = 8
DEFAULT_CELL_ROWS = 6


def column_label(index: int) -> str:
    return chr(ord("A") + index)


@dataclass(frozen=True, slots=True)
class Cell:
    """网格格子（可跨多格）。列用字母 A 起，行用数字 1 起；索引均为 0 起的闭区间。"""

    column_start: int
    row_start: int
    column_end: int
    row_end: int
    columns: int = DEFAULT_CELL_COLUMNS
    rows: int = DEFAULT_CELL_ROWS

    def __str__(self) -> str:
        start = f"{column_label(self.column_start)}{self.row_start + 1}"
        end = f"{column_label(self.column_end)}{self.row_end + 1}"
        name = start if start == end else f"{start}:{end}"
        layout = (
            ""
            if (self.columns, self.rows) == (DEFAULT_CELL_COLUMNS, DEFAULT_CELL_ROWS)
            else f", {self.columns}x{self.rows}"
        )
        return f"cell({name}{layout})"


@dataclass(frozen=True, slots=True)
class Path:
    """折线；kind=line 时恰好两个点。"""

    points: tuple[tuple[float, float], ...]
    kind: Literal["line", "path"] = "path"

    def __str__(self) -> str:
        joined = " -> ".join(f"{_number(x)},{_number(y)}" for x, y in self.points)
        return f"{self.kind}({joined})"


Shape = Union[Point, PixelId, Rect, Grid, Row, Col, Cell, Path]

SHAPE_NAMES: dict[type, str] = {
    Point: "point", PixelId: "pid", Rect: "rect", Grid: "grid",
    Row: "row", Col: "col", Cell: "cell", Path: "path",
}


def shape_name(shape: Shape) -> str:
    if isinstance(shape, Path):
        return shape.kind
    return SHAPE_NAMES[type(shape)]


@dataclass(frozen=True, slots=True)
class Address:
    """解析后的地址；`str(address)` 给出规范写法，可原样再次解析。"""

    time: TimeSpec | None = None
    step: int | None = None
    shapes: tuple[Shape, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        parts: list[str] = []
        if self.time is not None:
            parts.append(str(self.time))
        if self.step is not None:
            parts.append(f"step={self.step}")
        parts.extend(str(shape) for shape in self.shapes)
        return " ".join(parts)

    @property
    def is_empty(self) -> bool:
        return self.time is None and self.step is None and not self.shapes
