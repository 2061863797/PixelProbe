"""地址写法的解析器（手写，无额外依赖）。

写法由空格分隔的若干项组成，顺序不限：

    时间  f=7 | f=3..25 | f=3.. | f=..25 | f=0,5 | f=all
          t=0.5s | t=500ms | t=1:02.5 | t=1s..3s | t=0s,0.5s | t=all
    采样  step=3
    空间  point(x,y) pid(n) rect(x,y,w,h) grid(x,y,w,h step=4)
          row(y) col(x) cell(C4) cell(B2:C3) cell(C4, 8x6)
          line(x1,y1 -> x2,y2) path(x,y -> x,y -> ...)
    引用  @名称   （由调用方提供的引用表展开为另一段地址）
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from pixelprobe.address.model import (
    DEFAULT_CELL_COLUMNS,
    DEFAULT_CELL_ROWS,
    Address,
    Cell,
    Col,
    Grid,
    Path,
    PixelId,
    Point,
    Rect,
    Row,
    Shape,
    TimeSpec,
)
from pixelprobe.models.errors import AddressError

GRAMMAR_HINT = (
    "地址写法示例：\"f=120\"、\"t=1s..3s step=2\"、\"f=0,5\"、"
    "\"f=15 rect(4,4,10,10)\"、\"point(12,8) point(3,5)\"、\"row(120)\"、"
    "\"cell(C4)\"、\"grid(0,0,32,32 step=4)\"、\"@event3\""
)
MAX_REFERENCE_DEPTH = 4
MAX_CELL_LAYOUT = 26

_NAME = re.compile(r"[A-Za-z_]+")
_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_.-]*)")
_INT = re.compile(r"[+-]?\d+")
_FLOAT = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")
_TIME = re.compile(r"(\d+(?:\.\d*)?|\.\d+)(ms|s)?")
_TIMECODE = re.compile(r"(?:(\d+):)?(\d+):(\d{1,2}(?:\.\d*)?)")
_CELL = re.compile(r"([A-Za-z])(\d+)")
_LAYOUT = re.compile(r"(\d+)\s*[xX×]\s*(\d+)")


def _fail(text: str, position: int, message: str) -> AddressError:
    shown = text if len(text) <= 80 else text[:77] + "..."
    return AddressError(
        f"地址 {shown!r} 第 {position + 1} 个字符处：{message}", hint=GRAMMAR_HINT,
    )


class _Parser:
    def __init__(self, text: str, refs: Mapping[str, str] | None, depth: int) -> None:
        self.text = text
        self.refs = refs or {}
        self.depth = depth
        self.time: TimeSpec | None = None
        self.step: int | None = None
        self.shapes: list[Shape] = []

    # ---- 顶层扫描 ----

    def parse(self) -> Address:
        text, pos, n = self.text, 0, len(self.text)
        while pos < n:
            if text[pos].isspace():
                pos += 1
                continue
            start = pos
            if text[pos] == "@":
                match = _REF.match(text, pos)
                if match is None:
                    raise _fail(text, pos, "@ 后应为引用名称，例如 @event3")
                self._merge(self._reference(match.group(1), start), start)
                pos = match.end()
                continue
            match = _NAME.match(text, pos)
            if match is None:
                raise _fail(text, pos, f"无法识别 {text[pos]!r}")
            name = match.group().lower()
            pos = match.end()
            if pos < n and text[pos] == "=":
                end = pos + 1
                while end < n and not text[end].isspace():
                    end += 1
                self._assignment(name, text[pos + 1:end], start)
                pos = end
            elif pos < n and text[pos] == "(":
                close = text.find(")", pos)
                if close < 0:
                    raise _fail(text, pos, f"{name}( 缺少右括号")
                self.shapes.append(self._shape(name, text[pos + 1:close], start))
                pos = close + 1
            else:
                raise _fail(text, start, f"{name!r} 后应为 = 或 (")
        return Address(time=self.time, step=self.step, shapes=tuple(self.shapes))

    # ---- 引用 ----

    def _reference(self, name: str, position: int) -> Address:
        if name not in self.refs:
            known = "、".join(sorted(self.refs)) or "无"
            raise _fail(
                self.text, position,
                f"找不到引用 @{name}（可用引用：{known}；用 --ref-file 提供引用表）",
            )
        if self.depth >= MAX_REFERENCE_DEPTH:
            raise _fail(self.text, position, f"引用 @{name} 嵌套过深（可能循环引用）")
        return _Parser(self.refs[name], self.refs, self.depth + 1).parse()

    def _merge(self, other: Address, position: int) -> None:
        if other.time is not None:
            if self.time is not None:
                raise _fail(self.text, position, "时间被指定了两次")
            self.time = other.time
        if other.step is not None:
            if self.step is not None:
                raise _fail(self.text, position, "step 被指定了两次")
            self.step = other.step
        self.shapes.extend(other.shapes)

    # ---- 赋值项：f= t= step= ----

    def _assignment(self, name: str, value: str, position: int) -> None:
        if name in {"f", "t"}:
            if self.time is not None:
                raise _fail(self.text, position, "时间被指定了两次")
            self.time = (
                self._frames(value, position) if name == "f" else self._times(value, position)
            )
        elif name == "step":
            if self.step is not None:
                raise _fail(self.text, position, "step 被指定了两次")
            self.step = self._positive_int(value, position, "step")
        else:
            raise _fail(self.text, position, f"未知项 {name}=，可用 f=、t=、step=")

    def _positive_int(self, value: str, position: int, label: str) -> int:
        if not value.isdigit() or int(value) < 1:
            raise _fail(self.text, position, f"{label} 必须是正整数，收到 {value!r}")
        return int(value)

    def _frames(self, value: str, position: int) -> TimeSpec:
        def frame(item: str) -> int:
            if not item.isdigit():
                raise _fail(self.text, position, f"帧号必须是非负整数，收到 {item!r}")
            return int(item)

        return self._time_spec("frame", value, position, frame)

    def _times(self, value: str, position: int) -> TimeSpec:
        def seconds(item: str) -> float:
            code = _TIMECODE.fullmatch(item)
            if code is not None:
                hours = int(code.group(1) or 0)
                minutes = int(code.group(2))
                secs = float(code.group(3))
                if secs >= 60:
                    raise _fail(self.text, position, f"时间码秒数应小于 60，收到 {item!r}")
                return hours * 3600 + minutes * 60 + secs
            match = _TIME.fullmatch(item)
            if match is None:
                raise _fail(
                    self.text, position,
                    f"时间格式无法识别：{item!r}（可写 0.5s、500ms、1:02.5）",
                )
            number = float(match.group(1))
            return number / 1000.0 if match.group(2) == "ms" else number

        return self._time_spec("time", value, position, seconds)

    def _time_spec(self, kind, value: str, position: int, convert) -> TimeSpec:
        if value == "":
            raise _fail(self.text, position, "= 后缺少值")
        if value == "all":
            return TimeSpec(kind, "all")
        if ".." in value:
            if value.count("..") != 1 or "," in value:
                raise _fail(self.text, position, f"范围写法应为 a..b，收到 {value!r}")
            left, right = value.split("..")
            if left == "" and right == "":
                raise _fail(self.text, position, "范围至少要给出一端，或写 all")
            start = convert(left) if left else None
            end = convert(right) if right else None
            if start is not None and end is not None and end < start:
                raise _fail(self.text, position, f"范围结束早于开始：{value!r}")
            return TimeSpec(kind, "range", (start, end))
        if "," in value:
            items = value.split(",")
            if any(item == "" for item in items):
                raise _fail(self.text, position, f"列表中有空项：{value!r}")
            return TimeSpec(kind, "list", tuple(convert(item) for item in items))
        return TimeSpec(kind, "single", (convert(value),))

    # ---- 形状项 ----

    def _shape(self, name: str, body: str, position: int) -> Shape:
        handlers = {
            "point": self._point, "pid": self._pid, "rect": self._rect,
            "grid": self._grid, "row": self._row, "col": self._col,
            "cell": self._cell, "line": self._path, "path": self._path,
        }
        handler = handlers.get(name)
        if handler is None:
            raise _fail(
                self.text, position,
                f"未知形状 {name}(...)，可用：" + "、".join(f"{key}()" for key in handlers),
            )
        return handler(name, body.strip(), position)

    def _ints(self, name: str, body: str, position: int, count: int) -> list[int]:
        parts = [part.strip() for part in body.split(",")]
        if len(parts) != count or any(_INT.fullmatch(part) is None for part in parts):
            raise _fail(
                self.text, position,
                f"{name}(...) 需要 {count} 个整数，以逗号分隔，收到 {body!r}",
            )
        return [int(part) for part in parts]

    def _point(self, name: str, body: str, position: int) -> Point:
        x, y = self._ints(name, body, position, 2)
        return Point(x, y)

    def _pid(self, name: str, body: str, position: int) -> PixelId:
        (pixel_id,) = self._ints(name, body, position, 1)
        if pixel_id < 0:
            raise _fail(self.text, position, "pid 必须是非负整数")
        return PixelId(pixel_id)

    def _rect_values(self, name: str, body: str, position: int) -> tuple[int, int, int, int]:
        x, y, width, height = self._ints(name, body, position, 4)
        if width < 1 or height < 1:
            raise _fail(
                self.text, position, f"{name} 的宽高必须 >= 1，收到 {width}x{height}",
            )
        return x, y, width, height

    def _rect(self, name: str, body: str, position: int) -> Rect:
        return Rect(*self._rect_values(name, body, position))

    def _grid(self, name: str, body: str, position: int) -> Grid:
        step = None
        match = re.search(r"\s*,?\s*step\s*=\s*(\S+)\s*$", body)
        if match is not None:
            step = self._positive_int(match.group(1), position, "grid 的 step")
            body = body[:match.start()]
        return Grid(*self._rect_values(name, body, position), step=step)

    def _row(self, name: str, body: str, position: int) -> Row:
        (y,) = self._ints(name, body, position, 1)
        return Row(y)

    def _col(self, name: str, body: str, position: int) -> Col:
        (x,) = self._ints(name, body, position, 1)
        return Col(x)

    def _cell(self, name: str, body: str, position: int) -> Cell:
        name_part, _, layout_part = body.partition(",")
        columns, rows = DEFAULT_CELL_COLUMNS, DEFAULT_CELL_ROWS
        if layout_part.strip():
            layout = _LAYOUT.fullmatch(layout_part.strip())
            if layout is None:
                raise _fail(self.text, position, f"cell 网格规格应写成 8x6，收到 {layout_part.strip()!r}")
            columns, rows = int(layout.group(1)), int(layout.group(2))
            if not (1 <= columns <= MAX_CELL_LAYOUT and 1 <= rows <= 99):
                raise _fail(self.text, position, "cell 网格列数应为 1～26，行数 1～99")
        corners = [part.strip() for part in name_part.split(":")]
        if len(corners) not in {1, 2}:
            raise _fail(self.text, position, f"cell 名称应为 C4 或 B2:C3，收到 {name_part.strip()!r}")
        parsed: list[tuple[int, int]] = []
        for corner in corners:
            match = _CELL.fullmatch(corner)
            if match is None:
                raise _fail(self.text, position, f"cell 名称应为 列字母+行号，如 C4，收到 {corner!r}")
            column = ord(match.group(1).upper()) - ord("A")
            row = int(match.group(2)) - 1
            if not (0 <= column < columns and 0 <= row < rows):
                raise _fail(
                    self.text, position,
                    f"格子 {corner.upper()} 超出 {columns}x{rows} 网格"
                    f"（列 A～{chr(ord('A') + columns - 1)}，行 1～{rows}）",
                )
            parsed.append((column, row))
        (c0, r0), (c1, r1) = parsed[0], parsed[-1]
        return Cell(min(c0, c1), min(r0, r1), max(c0, c1), max(r0, r1), columns, rows)

    def _path(self, name: str, body: str, position: int) -> Path:
        points: list[tuple[float, float]] = []
        for part in body.split("->"):
            values = [value.strip() for value in part.split(",")]
            if len(values) != 2 or any(_FLOAT.fullmatch(value) is None for value in values):
                raise _fail(
                    self.text, position,
                    f"{name}(...) 的点应写成 x,y 并用 -> 连接，收到 {part.strip()!r}",
                )
            point = (float(values[0]), float(values[1]))
            if not all(math.isfinite(value) for value in point):
                raise _fail(self.text, position, f"{name} 的坐标必须是有限数值")
            points.append(point)
        if name == "line" and len(points) != 2:
            raise _fail(self.text, position, "line(...) 恰好需要两个点")
        if len(points) < 2:
            raise _fail(self.text, position, f"{name}(...) 至少需要两个点")
        return Path(tuple(points), kind="line" if name == "line" else "path")


def parse_address(text: str | None, refs: Mapping[str, str] | None = None) -> Address:
    """把地址文字解析为 Address；空文字返回空地址。"""
    if text is None or not text.strip():
        return Address()
    return _Parser(text, refs, depth=0).parse()
