"""一键媒体扫描：单遍解码同时产出概览网格、整帧变化曲线、事件与异常帧。

硬约束：整个扫描只允许打开一个 VideoReader、跑一次 iter_frames 循环。
循环体内同时完成：
1. 整帧（full 模式语义）相邻帧变化得分流式累计；
2. 网格图目标帧收集（帧号集合在循环前算好）；
3. 每帧亮度均值/标准差流式记录（黑帧/白帧/纯色帧判定）。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Callable

import numpy as np

from pixelprobe.core.change_detector import (
    ChangeEvent,
    ChangeRecord,
    segment_events,
)
from pixelprobe.core.contact_sheet import ContactSheetResult, compose_sheet
from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.models.media_info import MediaInfo

ProgressCallback = Callable[[int, int], None]

# 自动降采样目标：整段扫描最多解码约 1800 帧
_AUTO_SAMPLE_TARGET = 1800
# 异常帧判定阈值与列表上限
_BLACK_MAX = 16.0    # 全帧最大值低于此 → 黑帧（均值判定会把"黑底小亮块"误判）
_WHITE_MIN = 240.0   # 全帧最小值高于此 → 白帧
_FLAT_STD = 2.0
_MAX_ANOMALIES = 200


@dataclass
class ScanResult:
    """一键扫描结果。"""

    info: MediaInfo
    sheet: ContactSheetResult
    records: list[ChangeRecord]
    events: list[ChangeEvent]
    event_threshold: float
    anomalies: list[dict]
    anomalies_truncated: bool
    effective_sample_every: int
    frames_analyzed: int


def _plan_targets(frame_range: FrameRange, count: int) -> list[int]:
    """在采样帧序列（start + k*sample_every）上等距选 count 个帧号。"""
    if count < 1:
        raise InvalidRangeError(f"sheet_count {count} 无效，必须 >= 1")
    total = frame_range.count
    ks = np.linspace(0, total - 1, num=min(count, total)).round().astype(int)
    frames = [
        frame_range.start + int(k) * frame_range.sample_every
        for k in dict.fromkeys(ks.tolist())
    ]
    return frames


def scan_media(
    path: Path,
    sheet_count: int = 9,
    sample_every: int | None = None,
    event_threshold: float | None = None,
    tile_max_dim: int = 320,
    progress: ProgressCallback | None = None,
) -> ScanResult:
    """对未知视频做一遍概览扫描（信息 + 网格图 + 变化曲线 + 事件 + 异常帧）。"""
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()

        base_range = resolve_range(reader)
        if sample_every is None:
            sample_every = max(1, ceil(base_range.count / _AUTO_SAMPLE_TARGET))
        frame_range = resolve_range(reader, sample_every=sample_every)
        total = frame_range.count
        targets = set(_plan_targets(frame_range, sheet_count))

        records: list[ChangeRecord] = []
        anomalies: list[dict] = []
        truncated = False
        tiles: list[np.ndarray] = []
        tile_frames: list[int] = []
        tile_times: list[float] = []
        prev: np.ndarray | None = None
        prev_index: int | None = None
        done = 0

        def add_anomaly(kind: str, idx: int, t: float, value: float) -> None:
            nonlocal truncated
            if len(anomalies) >= _MAX_ANOMALIES:
                truncated = True
                return
            anomalies.append({
                "type": kind,
                "frame": idx,
                "time_seconds": t,
                "value": round(value, 4),
            })

        for idx, t, arr in reader.iter_frames(
            frame_range.start, frame_range.end, frame_range.sample_every
        ):
            # ① 整帧变化得分（与 detect_changes 的 full 模式同语义）
            if prev is not None and prev_index is not None:
                diff = np.maximum(arr, prev) - np.minimum(arr, prev)
                score = float(diff.mean())
                records.append(ChangeRecord(
                    frame=idx,
                    previous_frame=prev_index,
                    time_seconds=t,
                    score=round(score, 4),
                    normalized_score=round(score / 255.0, 6),
                ))
            prev = arr
            prev_index = idx

            # ② 网格图目标帧
            if idx in targets:
                tiles.append(arr.copy())
                tile_frames.append(idx)
                tile_times.append(t)

            # ③ 亮度异常帧
            vmax = float(arr.max())
            vmin = float(arr.min())
            if vmax < _BLACK_MAX:
                add_anomaly("black", idx, t, vmax)
            elif vmin > _WHITE_MIN:
                add_anomaly("white", idx, t, vmin)
            elif float(arr.std()) < _FLAT_STD:
                add_anomaly("flat", idx, t, float(arr.std()))

            done += 1
            if progress is not None:
                progress(done, total)

        if done == 0:
            raise DecodeError("指定范围内没有解码出任何帧")

    events, threshold_used = segment_events(records, threshold=event_threshold)
    # 孤立尖峰事件补充标记为闪帧候选
    for event in events:
        if event.record_count <= 2 and len(anomalies) < _MAX_ANOMALIES:
            anomalies.append({
                "type": "flash",
                "frame": event.peak_frame,
                "time_seconds": event.end_time,
                "value": event.peak_normalized,
            })

    sheet = compose_sheet(
        tiles, tile_frames, tile_times,
        tile_max_dim=tile_max_dim, annotate=True,
    )
    return ScanResult(
        info=info,
        sheet=sheet,
        records=records,
        events=events,
        event_threshold=threshold_used,
        anomalies=anomalies,
        anomalies_truncated=truncated,
        effective_sample_every=sample_every,
        frames_analyzed=done,
    )
