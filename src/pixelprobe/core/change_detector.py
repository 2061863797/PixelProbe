"""相邻帧变化检测与事件分段。

变化量定义：
- 单像素：D_t = |R_t - R_{t-1}| + |G_t - G_{t-1}| + |B_t - B_{t-1}|，
  归一化除以 765；
- 区域：D_t = mean(|I_t - I_{t-1}|)（对区域内所有像素和通道取平均），
  归一化除以 255；
- 网格：与区域语义一致，对全部采样点整体聚合
  （所有采样点、所有通道的绝对差取平均），归一化除以 255；
- 整帧（full，缺省模式）：与区域语义一致，对全画面取平均，归一化除以 255。
排名统一按得分降序，得分相同时帧号小的在前。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.utils.coordinates import (
    grid_points,
    validate_point,
    validate_rect,
)
from pixelprobe.utils.timecode import seconds_to_ms

ProgressCallback = Callable[[int, int], None]

ChangeMode = Literal["point", "rect", "grid", "full"]


@dataclass
class ChangeRecord:
    """一对相邻（采样）帧之间的变化量。"""

    frame: int
    previous_frame: int
    time_seconds: float
    score: float
    normalized_score: float

    def to_dict(self) -> dict:
        return {
            "frame": self.frame,
            "previous_frame": self.previous_frame,
            "time_seconds": self.time_seconds,
            "time_ms": seconds_to_ms(self.time_seconds),
            "score": self.score,
            "normalized_score": self.normalized_score,
        }


@dataclass
class ChangesResult:
    """变化检测结果（records 按帧号升序）。"""

    mode: ChangeMode
    records: list[ChangeRecord]
    frame_range: FrameRange
    frames_analyzed: int
    width: int
    height: int


def top_changes(records: list[ChangeRecord], top: int) -> list[ChangeRecord]:
    """取变化最大的前 top 条：得分降序，得分相同帧号升序。"""
    if top < 1:
        raise InvalidRangeError(f"top {top} 无效，必须 >= 1")
    return sorted(records, key=lambda r: (-r.score, r.frame))[:top]


def detect_changes(
    path: Path,
    point: tuple[int, int] | None = None,
    rect: tuple[int, int, int, int] | None = None,
    grid: tuple[int, int, int, int] | None = None,
    step: int | None = None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    progress: ProgressCallback | None = None,
) -> ChangesResult:
    """计算相邻（采样）帧之间的变化量。视频只解码一遍。"""
    chosen = [m for m, v in
              (("point", point), ("rect", rect), ("grid", grid)) if v is not None]
    if len(chosen) > 1:
        raise InvalidRangeError(
            "--point / --rect / --grid 最多只能指定一个（都不给时默认整帧）"
        )
    mode: ChangeMode = chosen[0] if chosen else "full"  # type: ignore[assignment]
    if mode != "grid" and step is not None:
        raise InvalidRangeError("--step 必须与 --grid 搭配使用")

    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        width, height = info.width, info.height

        xs = ys = None
        if mode == "point":
            assert point is not None
            validate_point(point[0], point[1], width, height)
        elif mode == "rect":
            assert rect is not None
            validate_rect(*rect, width, height)
        elif mode == "grid":
            assert grid is not None
            validate_rect(*grid, width, height)
            pts = grid_points(grid, step if step is not None else 1)
            xs = np.array([p[0] for p in pts], dtype=np.intp)
            ys = np.array([p[1] for p in pts], dtype=np.intp)

        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every
        )
        total = frame_range.count

        records: list[ChangeRecord] = []
        prev_values: np.ndarray | None = None
        prev_index: int | None = None
        done = 0
        for idx, t, arr in reader.iter_frames(
            frame_range.start, frame_range.end, frame_range.sample_every
        ):
            if mode == "point":
                assert point is not None
                cur = arr[point[1], point[0], :].copy()
            elif mode == "rect":
                assert rect is not None
                x, y, w, h = rect
                cur = np.ascontiguousarray(arr[y : y + h, x : x + w, :])
            elif mode == "grid":
                assert xs is not None and ys is not None
                cur = arr[ys, xs, :]
            else:
                # full：整帧本身即连续数组，iter_frames 每帧新建，无需拷贝
                cur = arr

            if prev_values is not None and prev_index is not None:
                # uint8 直接相减会下溢；max-min 与绝对差完全等价，且避免
                # 全画面转 int64 带来的大数组分配和内存带宽浪费。
                diff = np.maximum(cur, prev_values) - np.minimum(cur, prev_values)
                if mode == "point":
                    score = float(diff.sum())
                    normalized = score / 765.0
                else:
                    score = float(diff.mean())
                    normalized = score / 255.0
                records.append(
                    ChangeRecord(
                        frame=idx,
                        previous_frame=prev_index,
                        time_seconds=t,
                        score=round(score, 4),
                        normalized_score=round(normalized, 6),
                    )
                )
            prev_values = cur
            prev_index = idx
            done += 1
            if progress is not None:
                progress(done, total)

        if done == 0:
            raise DecodeError("指定范围内没有解码出任何帧")
        if done < 2:
            raise InvalidRangeError(
                "变化检测至少需要两帧，请扩大帧范围或减小 --sample-every"
            )
        return ChangesResult(
            mode=mode,
            records=records,
            frame_range=frame_range,
            frames_analyzed=done,
            width=width,
            height=height,
        )


@dataclass
class ChangeEvent:
    """一段连续高变化区间（帧号与时间均为闭区间）。"""

    start_frame: int
    end_frame: int
    start_time: float
    end_time: float
    peak_frame: int
    peak_score: float
    peak_normalized: float
    mean_normalized: float
    record_count: int

    def to_dict(self) -> dict:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "peak_frame": self.peak_frame,
            "peak_score": self.peak_score,
            "peak_normalized": self.peak_normalized,
            "mean_normalized": self.mean_normalized,
            "record_count": self.record_count,
        }


def segment_events(
    records: list[ChangeRecord],
    threshold: float | None = None,
    min_gap: int = 1,
    min_records: int = 1,
) -> tuple[list[ChangeEvent], float]:
    """把变化记录按阈值合并为事件区间，返回 (事件列表, 实际使用的阈值)。

    阈值作用于 normalized_score；缺省自动取 mean + 3*std（数据平坦时
    可能没有任何记录超阈，返回空列表）。相邻两条超阈记录在记录序列中的
    下标间隔 <= min_gap 时并入同一事件；记录数少于 min_records 的事件被丢弃。
    """
    if min_gap < 1:
        raise InvalidRangeError(f"min_gap {min_gap} 无效，必须 >= 1")
    if min_records < 1:
        raise InvalidRangeError(f"min_records {min_records} 无效，必须 >= 1")
    if not records:
        return [], threshold if threshold is not None else 0.0
    scores = np.array([r.normalized_score for r in records], dtype=np.float64)
    if threshold is None:
        threshold = float(scores.mean() + 3.0 * scores.std())
    above = [i for i, s in enumerate(scores) if s > threshold]

    groups: list[list[int]] = []
    for i in above:
        if groups and i - groups[-1][-1] <= min_gap:
            groups[-1].append(i)
        else:
            groups.append([i])

    events: list[ChangeEvent] = []
    for group in groups:
        if len(group) < min_records:
            continue
        members = [records[i] for i in group]
        peak = max(members, key=lambda r: (r.normalized_score, -r.frame))
        events.append(
            ChangeEvent(
                # 变化发生在 previous_frame → frame 之间，起点取前一帧
                start_frame=members[0].previous_frame,
                end_frame=members[-1].frame,
                start_time=members[0].time_seconds,
                end_time=members[-1].time_seconds,
                peak_frame=peak.frame,
                peak_score=peak.score,
                peak_normalized=peak.normalized_score,
                mean_normalized=round(
                    float(np.mean([r.normalized_score for r in members])), 6
                ),
                record_count=len(members),
            )
        )
    return events, round(float(threshold), 6)
