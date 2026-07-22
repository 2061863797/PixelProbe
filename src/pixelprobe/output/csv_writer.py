"""CSV 导出。统一 UTF-8（带 BOM，便于 Excel 直接打开中文路径文件）。"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from pixelprobe.core.change_detector import ChangeRecord
from pixelprobe.core.timeline_extractor import TimelineResult
from pixelprobe.utils.paths import atomic_output
from pixelprobe.utils.timecode import seconds_to_ms

_ENCODING = "utf-8-sig"


def write_timeline_csv(path: Path, result: TimelineResult) -> None:
    """时间线 CSV，每行：pixel_id,x,y,frame,time_seconds,time_ms,r,g,b。"""
    matrix: np.ndarray = result.matrix
    with atomic_output(Path(path)) as tmp:
        with tmp.open("w", encoding=_ENCODING, newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["pixel_id", "x", "y", "frame",
                 "time_seconds", "time_ms", "r", "g", "b"]
            )
            for ki, coord in enumerate(result.points):
                for ti, frame in enumerate(result.frames):
                    r, g, b = (int(v) for v in matrix[ki, ti])
                    t = result.times[ti]
                    writer.writerow(
                        [coord.pixel_id, coord.x, coord.y, frame,
                         t, seconds_to_ms(t), r, g, b]
                    )


def write_changes_csv(path: Path, records: list[ChangeRecord]) -> None:
    """变化检测 CSV（全部记录，按帧号升序）。"""
    with atomic_output(Path(path)) as tmp:
        with tmp.open("w", encoding=_ENCODING, newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["frame", "previous_frame", "time_seconds", "time_ms",
                 "score", "normalized_score"]
            )
            for rec in records:
                writer.writerow(
                    [rec.frame, rec.previous_frame, rec.time_seconds,
                     seconds_to_ms(rec.time_seconds),
                     rec.score, rec.normalized_score]
                )
