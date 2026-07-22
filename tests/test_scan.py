"""scan_media 一键扫描测试：单遍解码约束、事件与异常帧。"""

from __future__ import annotations

from pathlib import Path

from conftest import FLASH_FRAME, FRAME_COUNT, run_json
from generate_test_video import _encode_lossless_verified, make_frame
from pixelprobe.core import scan_media
from pixelprobe.core.video_reader import VideoReader


def test_scan_produces_overview(test_video: Path) -> None:
    result = scan_media(test_video, sheet_count=4)
    assert result.info.width == 32
    assert result.effective_sample_every == 1
    assert result.frames_analyzed == FRAME_COUNT
    assert len(result.records) == FRAME_COUNT - 1
    assert len(result.sheet.frames) == 4
    assert result.sheet.frames[0] == 0
    assert result.sheet.frames[-1] == FRAME_COUNT - 1
    # 闪白应被检出为事件，且作为孤立尖峰补充 flash 异常
    assert len(result.events) == 1
    event = result.events[0]
    assert event.start_frame <= FLASH_FRAME <= event.end_frame
    assert any(a["type"] == "flash" for a in result.anomalies)


def test_scan_single_decode_pass(test_video: Path, monkeypatch) -> None:
    """硬约束回归：整个扫描只允许打开一次 VideoReader。"""
    calls = {"open": 0}
    original_open = VideoReader.open

    def counting_open(self, path):
        calls["open"] += 1
        return original_open(self, path)

    monkeypatch.setattr(VideoReader, "open", counting_open)
    scan_media(test_video, sheet_count=3)
    assert calls["open"] == 1


def test_scan_auto_sample_every(test_video: Path) -> None:
    # 30 帧远小于 1800，自动采样应为 1；显式指定则生效
    auto = scan_media(test_video, sheet_count=2)
    assert auto.effective_sample_every == 1
    manual = scan_media(test_video, sheet_count=2, sample_every=3)
    assert manual.effective_sample_every == 3
    assert manual.frames_analyzed == 10


def test_scan_black_detection_not_fooled_by_small_bright_block(
    motion_video: Path,
) -> None:
    # 运动视频是黑底小白块：全帧均值虽低，但不应被误判为黑帧（判定用 max）
    result = scan_media(motion_video, sheet_count=2)
    assert all(a["type"] != "black" for a in result.anomalies)


def test_scan_single_frame_video(tmp_path: Path) -> None:
    """单帧视频：无变化记录也不报错，曲线导出安全跳过。"""
    video = _encode_lossless_verified(
        tmp_path / "单帧.mkv", [make_frame(0)], 30, "单帧"
    )
    result = scan_media(video, sheet_count=3)
    assert result.records == [] and result.events == []
    assert result.sheet.frames == [0]

    curve_out = tmp_path / "曲线.png"
    data = run_json(
        "scan", video, "--curve-output", curve_out, "--json"
    )["data"]
    assert data["curve_output_path"] is None
    assert not curve_out.exists()


def test_cli_scan_export(test_video: Path, tmp_path: Path) -> None:
    sheet_out = tmp_path / "概览.png"
    curve_out = tmp_path / "曲线.png"
    data = run_json(
        "scan", test_video, "--sheet-count", 4,
        "--sheet-output", sheet_out, "--curve-output", curve_out, "--json",
    )["data"]
    assert data["info"]["frame_count"] == FRAME_COUNT
    assert len(data["events"]) == 1
    assert sheet_out.exists() and curve_out.exists()
