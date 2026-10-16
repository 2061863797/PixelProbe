"""find 搜索语句的解析。

一条语句由一个或多个条件用 `|` 连接（并集）。条件写法：

    black | white | flat | flash | cut | duplicate      逐帧事件
    freeze >= 0.5s        freeze >= 15f                 画面停住至少多久（s、ms 或 f=帧）
    gap   gap >= 2x                                     相邻帧时间间隔超过中位间隔的倍数（缺省 1.5x）
    similar f=120   similar t=3.5s <= 4                 与参考帧相似（差分哈希距离，缺省 <= 6）
    frame.luma > 200   rect(0,0,50,50).r < 30            指标比较
    change in cell(B2)   first change in rect(...) > 12   区域变化事件（可给阈值，0～255）
    animation in rect(...)                               区域动画：开始变化到停稳的时长

指标：luma、r、g、b、std、min、max、diff（与上一帧的平均绝对差，0～255）；
整帧另有 sharpness。形状可写 point、pid、rect、cell，与地址写法相同。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Union

from pixelprobe.address import Address, parse_address
from pixelprobe.address.model import Cell, PixelId, Point, Rect, Shape, TimeSpec
from pixelprobe.models.errors import AddressError, QueryError

EVENTS = ("black", "white", "flat", "flash", "cut", "duplicate", "freeze", "gap")
FRAME_METRICS = ("luma", "r", "g", "b", "std", "min", "max", "diff", "sharpness", "blockiness")
REGION_METRICS = ("luma", "r", "g", "b", "std", "min", "max", "diff")
Operator = Literal[">", ">=", "<", "<=", "==", "!="]

QUERY_HINT = (
    '写法示例："black | flash"、"freeze >= 0.5s"、"similar f=120"、'
    '"frame.luma > 200"、"rect(0,0,50,50).r < 30"、"first change in cell(B2)"、'
    '"animation in rect(0,0,200,40)"'
)


@dataclass(frozen=True)
class EventTerm:
    kind: str
    # freeze：最短持续（数值 + 单位 s/f）；gap：间隔倍数
    amount: float | None = None
    unit: Literal["s", "f", "x"] | None = None

    def __str__(self) -> str:
        if self.amount is None:
            return self.kind
        return f"{self.kind} >= {self.amount:g}{self.unit}"


@dataclass(frozen=True)
class SimilarTerm:
    time: TimeSpec
    max_distance: int = 6

    def __str__(self) -> str:
        return f"similar {self.time} <= {self.max_distance}"


@dataclass(frozen=True)
class MetricTerm:
    shape: Shape | None  # None 表示整帧
    metric: str
    operator: Operator
    value: float

    def __str__(self) -> str:
        target = "frame" if self.shape is None else str(self.shape)
        return f"{target}.{self.metric} {self.operator} {self.value:g}"


@dataclass(frozen=True)
class ChangeTerm:
    shape: Shape
    threshold: float | None = None
    first: bool = False

    def __str__(self) -> str:
        text = f"{'first ' if self.first else ''}change in {self.shape}"
        return text if self.threshold is None else f"{text} > {self.threshold:g}"


@dataclass(frozen=True)
class AnimationTerm:
    """区域动画：从开始变化到停稳的完整过程，用于测量动画时长。"""

    shape: Shape

    def __str__(self) -> str:
        return f"animation in {self.shape}"


Term = Union[EventTerm, SimilarTerm, MetricTerm, ChangeTerm, AnimationTerm]

_SHAPE = r"[a-z]+\([^()]*\)"
# 关键字不区分大小写；形状原文交给地址解析（cell 的列字母需保留大小写）
_CHANGE = re.compile(rf"^(?P<first>first\s+)?change\s+in\s+(?P<shape>{_SHAPE})"
                     r"(?:\s*>\s*(?P<num>\d+(?:\.\d+)?))?$", re.IGNORECASE)
_ANIMATION = re.compile(rf"^animation\s+in\s+(?P<shape>{_SHAPE})$", re.IGNORECASE)
_SIMILAR = re.compile(r"^similar\s+(?P<time>[ft]=\S+)(?:\s*<=\s*(?P<bits>\d+))?$", re.IGNORECASE)
_EVENT = re.compile(r"^(?P<kind>[a-z]+)(?:\s*>=\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|f|x)?)?$",
                    re.IGNORECASE)
_METRIC = re.compile(rf"^(?P<target>frame|{_SHAPE})\.(?P<metric>[a-z]+)\s*"
                     r"(?P<op>>=|<=|==|!=|>|<)\s*(?P<num>-?\d+(?:\.\d+)?)$", re.IGNORECASE)


def _split(text: str) -> list[str]:
    """按括号外的 | 拆分。"""
    parts, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "|" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [" ".join(part.split()) for part in parts]


def _shape(text: str, allowed: tuple[type, ...]) -> Shape:
    try:
        address = parse_address(text)
    except AddressError as exc:
        raise QueryError(f"搜索语句中的位置 {text} 有误：{exc.message}") from exc
    if len(address.shapes) != 1 or address.time is not None or address.step is not None:
        raise QueryError(f"搜索语句中的位置 {text} 必须是单个形状")
    shape = address.shapes[0]
    if not isinstance(shape, allowed):
        names = "、".join({Point: "point", PixelId: "pid", Rect: "rect", Cell: "cell"}[t] for t in allowed)
        raise QueryError(f"{text} 不能用于这个条件；可用：{names}")
    return shape


def _term(text: str) -> Term:
    if not text:
        raise QueryError("搜索语句中有空条件", hint=QUERY_HINT)
    if match := _CHANGE.match(text):
        shape = _shape(match["shape"], (Point, PixelId, Rect, Cell))
        threshold = float(match["num"]) if match["num"] else None
        if threshold is not None and threshold > 255:
            raise QueryError("change 阈值是 0～255 的平均绝对差")
        return ChangeTerm(shape=shape, threshold=threshold, first=bool(match["first"]))
    if match := _ANIMATION.match(text):
        return AnimationTerm(shape=_shape(match["shape"], (Rect, Cell)))
    if match := _SIMILAR.match(text):
        try:
            address: Address = parse_address(match["time"].lower())
        except AddressError as exc:
            raise QueryError(f"similar 的参考时间有误：{exc.message}") from exc
        if address.time is None or address.time.mode != "single" or address.shapes:
            raise QueryError("similar 需要单个参考帧，如 similar f=120 或 similar t=3.5s")
        bits = int(match["bits"]) if match["bits"] else 6
        if bits > 64:
            raise QueryError("similar 的距离是 0～64 的哈希位数")
        return SimilarTerm(time=address.time, max_distance=bits)
    if match := _METRIC.match(text):
        shape = None
        if match["target"].lower() != "frame":
            shape = _shape(match["target"], (Point, PixelId, Rect, Cell))
        metric = match["metric"].lower()
        metrics = FRAME_METRICS if shape is None else REGION_METRICS
        if metric not in metrics:
            raise QueryError(f"未知指标 {metric}；可用：{'、'.join(metrics)}")
        return MetricTerm(shape=shape, metric=metric, operator=match["op"],  # type: ignore[arg-type]
                          value=float(match["num"]))
    if match := _EVENT.match(text):
        kind = match["kind"].lower()
        if kind not in EVENTS:
            raise QueryError(f"未知条件 {kind}", hint=QUERY_HINT)
        number, unit = match["num"], (match["unit"] or "").lower() or None
        if kind == "freeze":
            if number is None:
                return EventTerm("freeze", 0.5, "s")
            if unit not in {"s", "ms", "f"}:
                raise QueryError("freeze 的时长需要单位：s、ms 或 f（帧），如 freeze >= 0.5s")
            amount = float(number) / 1000 if unit == "ms" else float(number)
            return EventTerm("freeze", amount, "f" if unit == "f" else "s")
        if kind == "gap":
            if number is None:
                return EventTerm("gap", 1.5, "x")
            if unit not in {None, "x"} or float(number) <= 1:
                raise QueryError("gap 的倍数需大于 1，如 gap >= 2x")
            return EventTerm("gap", float(number), "x")
        if number is not None:
            raise QueryError(f"{kind} 不接受数值条件")
        return EventTerm(kind)
    raise QueryError(f"无法理解条件：{text}", hint=QUERY_HINT)


def parse_query(text: str) -> list[Term]:
    """解析搜索语句；返回按书写顺序排列的条件列表（结果取并集）。"""
    if not text or not text.strip():
        raise QueryError("搜索语句为空", hint=QUERY_HINT)
    if len(text) > 1000:
        raise QueryError("搜索语句过长（最多 1000 个字符）")
    terms = [_term(part) for part in _split(text)]
    if len(terms) > 16:
        raise QueryError("一条搜索语句最多 16 个条件")
    return terms


def needs_decode(terms: list[Term]) -> bool:
    """是否含有必须读取原始像素的条件（区域指标与区域变化）；整帧条件可完全由索引回答。"""
    return any(
        isinstance(term, (ChangeTerm, AnimationTerm))
        or (isinstance(term, MetricTerm) and term.shape is not None)
        for term in terms
    )
