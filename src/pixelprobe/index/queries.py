"""直接由索引回答的查询。

原则：只在结果与直接解码**逐值一致**时才使用索引；任何一项条件不满足就返回 None，
由调用方退回原始解码。索引中的 diff_prev 是相邻展示帧（间隔 1）的差异，因此
只能回答 sample_every == 1 的问题。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from pixelprobe.core.change_detector import ChangeRecord
from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.index.store import MediaIndex


@dataclass(frozen=True)
class IndexedRange:
    """索引中某个闭区间帧范围对应的行。"""

    rows: np.ndarray  # 行号（升序）
    frames: np.ndarray
    times: np.ndarray


def rows_for(index: MediaIndex, frame_range: FrameRange) -> IndexedRange | None:
    """取闭区间内的全部帧；要求逐帧连续、无缺帧，否则返回 None。"""
    if frame_range.sample_every != 1:
        return None
    frames = index.frames["frame"]
    first = int(np.searchsorted(frames, frame_range.start, side="left"))
    last = int(np.searchsorted(frames, frame_range.end, side="right"))
    selected = frames[first:last]
    expected = frame_range.end - frame_range.start + 1
    if len(selected) != expected or (expected and (
        selected[0] != frame_range.start or selected[-1] != frame_range.end
    )):
        return None
    rows = np.arange(first, last)
    return IndexedRange(rows=rows, frames=selected, times=index.frames["time_seconds"][first:last])


def full_frame_changes(index: MediaIndex, frame_range: FrameRange) -> list[ChangeRecord] | None:
    """整帧变化记录，与 detect_changes 整帧模式逐值相同；无法保证时返回 None。"""
    selected = rows_for(index, frame_range)
    if selected is None or len(selected.rows) < 2:
        return None
    diffs = index.frames["diff_prev"]
    records: list[ChangeRecord] = []
    for position in range(1, len(selected.rows)):
        row = int(selected.rows[position])
        score = float(diffs[row])
        records.append(ChangeRecord(
            frame=int(selected.frames[position]),
            previous_frame=int(selected.frames[position - 1]),
            time_seconds=float(selected.times[position]),
            score=round(score, 4),
            normalized_score=round(score / 255.0, 6),
        ))
    return records


def brightness_anomalies(
    index: MediaIndex, rows: np.ndarray, *, black_max: float, white_min: float,
    flat_std: float, limit: int,
) -> tuple[list[dict], bool]:
    """按 scan 的规则（黑帧 → 白帧 → 纯色帧，逐帧先到先记）列出亮度异常帧。"""
    anomalies: list[dict] = []
    truncated = False
    columns = index.frames
    for row in rows:
        row = int(row)
        frame = int(columns["frame"][row])
        time_seconds = float(columns["time_seconds"][row])
        vmax, vmin, std = float(columns["max"][row]), float(columns["min"][row]), float(columns["std"][row])
        if vmax < black_max:
            kind, value = "black", vmax
        elif vmin > white_min:
            kind, value = "white", vmin
        elif std < flat_std:
            kind, value = "flat", std
        else:
            continue
        if len(anomalies) >= limit:
            truncated = True
            continue
        anomalies.append({
            "type": kind, "frame": frame, "time_seconds": time_seconds,
            "value": round(value, 4),
        })
    return anomalies, truncated
