"""find 搜索语句的求值。

数据来源：
- 整帧条件使用每帧体检值（与索引 L1 同一函数计算）。有完整覆盖所选范围的索引时直接查表，
  否则顺序解码一遍现算；两条路径逐值相同（范围起点之前多解码一帧，使首帧的 diff 也有前帧）。
- 区域条件（区域指标、区域变化）必须读取原始像素，总是解码。

所有结果都按"帧号区间"给出：连续满足条件的帧合并为一个区间，value 为区间内最能说明
条件的值（大于类取最大、小于类取最小）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isnan
from pathlib import Path

import numpy as np

from pixelprobe.address import Address, points_of, range_options, rect_of
from pixelprobe.address.model import Point, PixelId, Shape
from pixelprobe.core.change_detector import ChangeRecord, segment_events
from pixelprobe.core.color import luminance_array
from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.find.query import ChangeTerm, EventTerm, MetricTerm, SimilarTerm, Term, needs_decode
from pixelprobe.index.builder import frame_columns, frame_metrics
from pixelprobe.models.errors import DecodeError, InvalidRangeError

# 与 scan 相同的亮度异常阈值
BLACK_MAX = 16
WHITE_MIN = 240
FLAT_STD = 2.0
# 画面"停住"：与上一帧的整帧平均绝对差不超过此值（0～255）
FREEZE_TOLERANCE = 0.1
# 切换/闪帧的"跳变"：与上一帧的平均绝对差 / 255 不低于此值（参考 PySceneDetect 的固定阈值思路）
JUMP_THRESHOLD = 0.1
# 闪帧前后两帧"相同"：差分哈希距离与通道均值差的上限（纯色画面的哈希都为 0，需同时比较颜色）
FLASH_SIMILAR_BITS = 10
FLASH_COLOR_DELTA = 8.0
_LUMA_WEIGHTS = luminance_array(np.eye(3, dtype=np.uint8))
_FRAME_COLUMN = {
    "luma": "luma_mean", "r": "mean_r", "g": "mean_g", "b": "mean_b", "std": "std",
    "min": "min", "max": "max", "diff": "diff_prev", "sharpness": "sharpness",
}
_OPERATORS: dict[str, Callable[[np.ndarray, float], np.ndarray]] = {
    ">": np.greater, ">=": np.greater_equal, "<": np.less, "<=": np.less_equal,
    "==": np.equal, "!=": np.not_equal,
}

DEFINITIONS = {
    "black": f"全帧最大值 < {BLACK_MAX}（与 scan 相同）",
    "white": f"全帧最小值 > {WHITE_MIN}，且不是黑帧",
    "flat": f"三通道标准差 < {FLAT_STD}，且不是黑帧或白帧",
    "duplicate": "与上一帧逐像素完全相同（平均绝对差 = 0）",
    "freeze": "连续多帧与上一帧的整帧平均绝对差 <= freeze_tolerance（缺省 "
              f"{FREEZE_TOLERANCE}）；区间从停住的第一帧算起，时长量到下一帧出现为止。"
              "整帧平均会稀释小面积变化（如大画面中移动的光标），局部是否停住请用 change in <区域>",
    "gap": "与上一帧的时间间隔超过所选范围中位间隔的倍数；value 为倍数（掉帧、卡顿候选）",
    "cut": "跳变 = 与上一帧的平均绝对差 / 255 >= jump_threshold（缺省 0.1）。连续一到两次跳变且不是闪帧"
           "即为切换；区间为切换后的第一帧，jumps 为跳变次数（2 表示经过一帧过渡）。"
           "连续三次以上跳变视为渐变或运动，不计入",
    "flash": f"恰好连续两次跳变，且跳变前后两帧差分哈希距离 <= {FLASH_SIMILAR_BITS}、"
             f"通道均值差 <= {FLASH_COLOR_DELTA:g}；区间为中间被替换的帧",
    "similar": "与参考帧的 64 位差分哈希（9×8 亮度）汉明距离不超过给定值；value 为最小距离。"
               "哈希只反映亮度起伏的结构，纯色画面之间的距离都为 0",
    "metric": "指标比较；luma 为 Rec.709 权重亮度，r/g/b 为通道均值，std/min/max 为三通道合计，"
              "diff 为与上一帧的平均绝对差（0～255），sharpness 为缩小到长边 256 后的拉普拉斯方差",
    "change": "区域内与上一帧的平均绝对差（0～255）超过阈值；未给阈值时按 changes 的自动阈值分段",
}


@dataclass
class FindResult:
    frame_range: FrameRange
    frames_analyzed: int
    computed_from: str
    matches: list[dict]
    truncated: bool
    kinds: list[str]
    series: dict[str, np.ndarray] = field(default_factory=dict)


@dataclass
class _Table:
    columns: dict[str, np.ndarray]
    regions: dict[str, dict[str, np.ndarray]]

    @property
    def frames(self) -> np.ndarray:
        return self.columns["frame"]

    @property
    def times(self) -> np.ndarray:
        return self.columns["time_seconds"]


def _region_key(shape: Shape) -> str:
    return str(shape)


def _region_values(arr: np.ndarray, area: tuple, previous: np.ndarray | None) -> tuple[dict, np.ndarray]:
    kind, value = area
    if kind == "point":
        x, y = value
        crop = arr[y: y + 1, x: x + 1, :]
    else:
        x, y, w, h = value
        crop = arr[y: y + h, x: x + w, :]
    crop = np.ascontiguousarray(crop)
    mean_rgb = crop.mean(axis=(0, 1))
    diff = float("nan") if previous is None else float(
        (np.maximum(crop, previous) - np.minimum(crop, previous)).mean()
    )
    return {
        "r": mean_rgb[0], "g": mean_rgb[1], "b": mean_rgb[2],
        "luma": float(mean_rgb @ _LUMA_WEIGHTS), "std": float(crop.std()),
        "min": int(crop.min()), "max": int(crop.max()), "diff": diff,
    }, crop


def _areas(terms: list[Term], width: int, height: int) -> dict[str, tuple]:
    areas: dict[str, tuple] = {}
    for term in terms:
        shape = getattr(term, "shape", None)
        if shape is None or not isinstance(term, (MetricTerm, ChangeTerm)):
            continue
        address = Address(shapes=(shape,))
        if isinstance(shape, (Point, PixelId)):
            areas[_region_key(shape)] = ("point", points_of(address, width, height)[0])
        else:
            areas[_region_key(shape)] = ("rect", rect_of(address, width, height))
    return areas


def _decode_table(
    reader: VideoReader, frame_range: FrameRange, areas: dict[str, tuple],
    *, progress, cancel_check,
) -> _Table:
    start = max(0, frame_range.start - 1)
    columns: dict[str, list] = {}
    regions: dict[str, dict[str, list]] = {key: {} for key in areas}
    previous: np.ndarray | None = None
    previous_crops: dict[str, np.ndarray | None] = {key: None for key in areas}
    done = 0
    for index, timestamp, arr in reader.iter_frames(start, frame_range.end, 1):
        if cancel_check is not None:
            cancel_check()
        if index >= frame_range.start:
            row, _ = frame_metrics(index, timestamp, arr, previous)
            for name, value in row.items():
                columns.setdefault(name, []).append(value)
        for key, area in areas.items():
            values, crop = _region_values(arr, area, previous_crops[key])
            if index >= frame_range.start:
                for name, value in values.items():
                    regions[key].setdefault(name, []).append(value)
            previous_crops[key] = crop
        previous = arr
        if index >= frame_range.start:
            done += 1
            if progress is not None:
                progress(done, frame_range.count)
    if not columns:
        raise DecodeError("指定范围内没有解码出任何帧")
    return _Table(
        columns=frame_columns(columns),
        regions={key: {name: np.asarray(values, dtype=np.float64) for name, values in series.items()}
                 for key, series in regions.items()},
    )


def _intervals(mask: np.ndarray) -> list[tuple[int, int]]:
    """连续为真的行区间（闭区间，行号）。"""
    result: list[tuple[int, int]] = []
    start = None
    for row, flag in enumerate(mask):
        if flag and start is None:
            start = row
        elif not flag and start is not None:
            result.append((start, row - 1))
            start = None
    if start is not None:
        result.append((start, len(mask) - 1))
    return result


def _match(table: _Table, term: Term, kind: str, first: int, last: int, value, **extra) -> dict:
    frames, times = table.frames, table.times
    payload = {
        "term": str(term), "kind": kind,
        "start_frame": int(frames[first]), "end_frame": int(frames[last]),
        "start_time": round(float(times[first]), 6), "end_time": round(float(times[last]), 6),
        "frame_count": int(last - first + 1),
        "value": None if value is None or (isinstance(value, float) and isnan(value)) else (
            round(float(value), 6) if isinstance(value, (float, np.floating)) else int(value)
        ),
    }
    payload.update(extra)
    return payload


def _extreme(values: np.ndarray, operator: str, first: int, last: int) -> float:
    window = values[first: last + 1]
    if operator in {">", ">="}:
        return float(np.nanmax(window))
    if operator in {"<", "<="}:
        return float(np.nanmin(window))
    return float(window[0])


def _hamming(hashes: np.ndarray, reference: int) -> np.ndarray:
    xor = np.bitwise_xor(hashes.astype(np.uint64), np.uint64(reference))
    return np.unpackbits(xor.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1)


def _records(frames: np.ndarray, times: np.ndarray, diffs: np.ndarray) -> list[ChangeRecord]:
    return [
        ChangeRecord(
            frame=int(frames[row]), previous_frame=int(frames[row - 1]),
            time_seconds=float(times[row]), score=round(float(diffs[row]), 4),
            normalized_score=round(float(diffs[row]) / 255.0, 6),
        )
        for row in range(1, len(frames)) if not isnan(float(diffs[row]))
    ]


def _event_matches(
    table: _Table, term: EventTerm, jump_threshold: float, freeze_tolerance: float,
) -> list[dict]:
    columns, frames, times = table.columns, table.frames, table.times
    kind = term.kind
    if kind in {"black", "white", "flat"}:
        black = columns["max"] < BLACK_MAX
        white = (columns["min"] > WHITE_MIN) & ~black
        flat = (columns["std"] < FLAT_STD) & ~black & ~white
        mask, column, reducer = {
            "black": (black, "max", np.max), "white": (white, "min", np.min),
            "flat": (flat, "std", np.min),
        }[kind]
        return [_match(table, term, kind, a, b, reducer(columns[column][a: b + 1]))
                for a, b in _intervals(mask)]
    diffs = columns["diff_prev"]
    if kind == "duplicate":
        return [_match(table, term, kind, a, b, b - a + 1) for a, b in _intervals(diffs == 0)]
    if kind == "freeze":
        matches = []
        steps = np.diff(times)
        typical = float(np.median(steps)) if len(steps) else 0.0
        for a, b in _intervals(diffs <= freeze_tolerance):
            # a 帧与前一帧相同：停住的画面从前一帧开始显示
            first = a - 1 if a > 0 else a
            shown = b - first + 1
            end_time = float(times[b + 1]) if b + 1 < len(times) else float(times[b]) + typical
            duration = end_time - float(times[first])
            enough = shown >= term.amount if term.unit == "f" else duration >= (term.amount or 0) - 1e-9
            if enough:
                matches.append(_match(table, term, kind, first, b, duration, duration_seconds=round(duration, 6)))
        return matches
    if kind == "gap":
        steps = np.diff(times)
        if len(steps) < 2:
            return []
        typical = float(np.median(steps))
        if typical <= 0:
            return []
        ratio = np.concatenate([[0.0], steps / typical])
        return [
            _match(table, term, kind, a, b, float(ratio[a: b + 1].max()),
                   interval_seconds=round(float(steps[a - 1: b].max()), 6),
                   typical_interval_seconds=round(typical, 6))
            for a, b in _intervals(ratio > (term.amount or 1.5))
        ]
    # cut / flash：连续"跳变"帧（与上一帧的归一化差异 >= jump_threshold）组成一段
    with np.errstate(invalid="ignore"):
        jumps = (diffs / 255.0 >= jump_threshold) & ~np.isnan(diffs)
    matches = []
    for a, b in _intervals(jumps):
        count = b - a + 1
        before, after = a - 1, b
        if count > 2 or before < 0:
            continue  # 超过两次连续跳变是渐变或运动；首行的前一帧不在范围内无法判断
        peak = float(diffs[a: b + 1].max()) / 255.0
        # 闪帧：恰好两次跳变且前后两帧相同（哈希相近且平均颜色相近）；否则是一次切换
        distance = int(_hamming(columns["dhash"][[after]], int(columns["dhash"][before]))[0])
        color_delta = max(abs(float(columns[name][after]) - float(columns[name][before]))
                          for name in ("mean_r", "mean_g", "mean_b"))
        restored = distance <= FLASH_SIMILAR_BITS and color_delta <= FLASH_COLOR_DELTA
        if kind == "flash" and count == 2 and restored:
            matches.append(_match(table, term, kind, a, a, peak,
                                  before_after_distance=distance,
                                  before_after_color_delta=round(color_delta, 4)))
        elif kind == "cut" and not (count == 2 and restored):
            matches.append(_match(table, term, kind, after, after, peak, jumps=count))
    return matches


def _reference_hash(reader: VideoReader, table: _Table, index, term: SimilarTerm) -> tuple[int, int]:
    """返回 (参考帧号, 差分哈希)；优先用已有数据，否则只解码该帧。"""
    value = term.time.values[0]
    if term.time.kind == "frame":
        frame = int(value)  # type: ignore[arg-type]
        reader.validate_frame_index(frame)
        timestamp, arr = None, None
    else:
        reader.validate_time(float(value))  # type: ignore[arg-type]
        frame, timestamp, arr = reader.get_frame_by_time(float(value))  # type: ignore[arg-type]
    for source in (table.columns, getattr(index, "frames", None)):
        if source is None:
            continue
        hits = np.nonzero(source["frame"] == frame)[0]
        if len(hits):
            return frame, int(source["dhash"][hits[0]])
    if arr is None:
        timestamp, arr = reader.get_frame_by_index(frame)
    row, _ = frame_metrics(frame, float(timestamp or 0.0), arr, None)
    return frame, int(row["dhash"])


def find(
    path: Path,
    terms: list[Term],
    address: Address,
    *,
    index=None,
    jump_threshold: float = JUMP_THRESHOLD,
    freeze_tolerance: float = FREEZE_TOLERANCE,
    limit: int = 200,
    progress=None,
    cancel_check=None,
) -> FindResult:
    """在地址给定的时间范围内求值搜索语句（各条件结果取并集）。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        options = range_options(address)
        options.pop("sample_every", None)
        frame_range = resolve_range(reader, **options)
        areas = _areas(terms, info.width, info.height)
        table = None
        computed_from = "decode"
        if index is not None and not needs_decode(terms):
            from pixelprobe.index.queries import rows_for
            selected = rows_for(index, frame_range)
            if selected is not None:
                table = _Table(
                    columns={name: values[selected.rows] for name, values in index.frames.items()},
                    regions={},
                )
                computed_from = "index"
        if table is None:
            table = _decode_table(reader, frame_range, areas, progress=progress, cancel_check=cancel_check)
        matches: list = []
        kinds: list[str] = []
        for order, term in enumerate(terms):
            found: list[dict]
            if isinstance(term, EventTerm):
                kinds.append(term.kind)
                found = _event_matches(table, term, jump_threshold, freeze_tolerance)
            elif isinstance(term, SimilarTerm):
                kinds.append("similar")
                reference, digest = _reference_hash(reader, table, index, term)
                distance = _hamming(table.columns["dhash"], digest)
                found = [
                    _match(table, term, "similar", a, b, int(distance[a: b + 1].min()), reference_frame=reference)
                    for a, b in _intervals(distance <= term.max_distance)
                ]
            elif isinstance(term, MetricTerm):
                kinds.append("metric")
                if term.shape is None:
                    values = table.columns[_FRAME_COLUMN[term.metric]].astype(np.float64)
                else:
                    values = table.regions[_region_key(term.shape)][term.metric]
                with np.errstate(invalid="ignore"):
                    mask = _OPERATORS[term.operator](values, term.value) & ~np.isnan(values)
                found = [_match(table, term, "metric", a, b, _extreme(values, term.operator, a, b))
                         for a, b in _intervals(mask)]
            else:
                kinds.append("change")
                diffs = table.regions[_region_key(term.shape)]["diff"]
                if term.threshold is not None:
                    with np.errstate(invalid="ignore"):
                        mask = (diffs > term.threshold) & ~np.isnan(diffs)
                    found = [_match(table, term, "change", a, b, float(np.nanmax(diffs[a: b + 1])))
                             for a, b in _intervals(mask)]
                else:
                    records = _records(table.frames, table.times, diffs)
                    events, threshold = segment_events(records) if records else ([], None)
                    row_of = {int(frame): row for row, frame in enumerate(table.frames)}
                    found = [
                        _match(table, term, "change", row_of[event.start_frame], row_of[event.end_frame],
                               event.peak_score, peak_frame=event.peak_frame,
                               threshold_used=round(float(threshold), 6))
                        for event in events
                    ]
                if term.first:
                    found = found[:1]
            matches.extend((order, item) for item in found)
    # 按时间先后排列；同一帧起点的结果按条件书写顺序
    matches = [item for _, item in sorted(matches, key=lambda pair: (pair[1]["start_frame"], pair[0]))]
    truncated = len(matches) > limit
    if len(table.frames) == 0:
        raise InvalidRangeError("所选范围内没有帧")
    return FindResult(
        frame_range=frame_range, frames_analyzed=int(len(table.frames)),
        computed_from=computed_from, matches=matches[:limit], truncated=truncated,
        kinds=list(dict.fromkeys(kinds)),
    )
