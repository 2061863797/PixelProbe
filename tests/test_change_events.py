"""detect_changes full 模式与事件分段测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from conftest import FLASH_FRAME, FRAME_COUNT, run_json, run_json_error
from pixelprobe.core import detect_changes, segment_events
from pixelprobe.core.change_detector import ChangeRecord
from pixelprobe.models.errors import InvalidRangeError


def _record(frame: int, normalized: float) -> ChangeRecord:
    return ChangeRecord(
        frame=frame,
        previous_frame=frame - 1,
        time_seconds=frame / 30,
        score=normalized * 255,
        normalized_score=normalized,
    )


def test_full_mode_is_default(test_video: Path) -> None:
    result = detect_changes(test_video)
    assert result.mode == "full"
    assert result.frames_analyzed == FRAME_COUNT
    top = max(result.records, key=lambda r: r.normalized_score)
    assert top.frame in (FLASH_FRAME, FLASH_FRAME + 1)


def test_full_mode_matches_whole_frame_rect(test_video: Path) -> None:
    # full 模式与显式整幅 rect 的得分序列完全一致
    full = detect_changes(test_video)
    rect = detect_changes(test_video, rect=(0, 0, 32, 32))
    assert [r.score for r in full.records] == [r.score for r in rect.records]


def test_two_targets_still_rejected(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError):
        detect_changes(test_video, point=(0, 0), rect=(0, 0, 4, 4))


def test_auto_threshold_finds_flash_event(test_video: Path) -> None:
    result = detect_changes(test_video)
    events, threshold = segment_events(result.records)
    assert threshold > 0
    assert len(events) == 1
    event = events[0]
    # 闪白进入(15)与退出(16)合并为一个事件，峰值在其中
    assert event.start_frame <= FLASH_FRAME <= event.end_frame
    assert event.peak_frame in (FLASH_FRAME, FLASH_FRAME + 1)
    assert event.record_count == 2


def test_explicit_threshold_and_min_records() -> None:
    records = [
        _record(1, 0.01), _record(2, 0.9), _record(3, 0.01),
        _record(4, 0.8), _record(5, 0.85), _record(6, 0.01),
    ]
    events, threshold = segment_events(records, threshold=0.5)
    assert threshold == 0.5
    # 帧 2 与帧 4-5 之间隔了一条低分记录（下标差 2 > min_gap=1）→ 两个事件
    assert [(e.start_frame, e.end_frame) for e in events] == [(1, 2), (3, 5)]
    # min_records=2 过滤掉单记录事件
    events2, _ = segment_events(records, threshold=0.5, min_records=2)
    assert [(e.start_frame, e.end_frame) for e in events2] == [(3, 5)]
    # min_gap=2 时两段合并
    events3, _ = segment_events(records, threshold=0.5, min_gap=2)
    assert [(e.start_frame, e.end_frame) for e in events3] == [(1, 5)]


def test_segment_events_empty_and_flat() -> None:
    assert segment_events([]) == ([], 0.0)
    flat = [_record(i, 0.5) for i in range(1, 6)]
    events, _ = segment_events(flat)  # 全平序列：无人超阈
    assert events == []


def test_auto_threshold_works_on_small_samples() -> None:
    """记录数 <= 10 时尖峰仍须可检出（阈值剔除最大值后计算）。

    若用全部记录算 mean+3*std，n 个样本最大 z 分数上界 (n-1)/sqrt(n)，
    n<=10 时任何尖峰都不可能超阈。
    """
    records = [_record(i, 0.005) for i in range(1, 10)] + [_record(10, 0.9)]
    events, threshold = segment_events(records)
    assert len(events) == 1
    assert events[0].peak_frame == 10
    assert events[0].peak_time == records[-1].time_seconds
    assert 0.005 <= threshold < 0.9  # 背景恒值时阈值恰为背景值（std=0）
    # 单条记录：无对比基准，不产生事件也不报错
    single, threshold_single = segment_events([_record(1, 0.9)])
    assert single == [] and threshold_single == 0.9


def test_cli_changes_default_full_with_events(test_video: Path) -> None:
    data = run_json("changes", test_video, "--json")["data"]
    assert data["mode"] == "full"
    assert data["event_threshold_used"] > 0
    assert len(data["events"]) == 1
    assert (
        data["events"][0]["start_frame"]
        <= FLASH_FRAME
        <= data["events"][0]["end_frame"]
    )


def test_cli_curve_image_export(test_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "曲线.png"
    data = run_json(
        "changes", test_video, "--curve-image", out, "--json"
    )["data"]
    assert data["curve_image_path"] == str(out)
    with Image.open(out) as image:
        assert image.format == "PNG"
        assert image.size == (768, 256)


def test_cli_step_still_requires_grid(test_video: Path) -> None:
    code, data = run_json_error(
        "changes", test_video, "--step", 2, "--json"
    )
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"
