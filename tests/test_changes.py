"""pixelprobe changes 测试：闪白峰值定位、三种模式、CSV。"""

from __future__ import annotations

import csv as csv_module
from pathlib import Path

from conftest import (
    COUNTER_POS,
    FLASH_FRAME,
    FRAME_COUNT,
    GREEN_POS,
    run_json,
    run_json_error,
)
from pixelprobe.core import detect_changes, top_changes


def test_flash_frame_has_max_score_rect(test_video: Path) -> None:
    # 第 15 帧闪白时变化得分最高
    data = run_json(
        "changes", test_video, "--rect", "0,0,32,32", "--top", 3, "--json"
    )["data"]
    assert data["top"][0]["frame"] == FLASH_FRAME
    assert data["top"][0]["previous_frame"] == FLASH_FRAME - 1
    # 闪白进入和退出占据前两名
    assert {data["top"][0]["frame"], data["top"][1]["frame"]} == {
        FLASH_FRAME, FLASH_FRAME + 1
    }


def test_point_mode_green_pixel(test_video: Path) -> None:
    gx, gy = GREEN_POS
    data = run_json(
        "changes", test_video, "--point", f"{gx},{gy}", "--top", 2, "--json"
    )["data"]
    assert data["mode"] == "point"
    # 绿→白：|255-0|+|255-255|+|255-0| = 510，归一化 510/765
    for rec in data["top"]:
        assert rec["score"] == 510.0
        assert abs(rec["normalized_score"] - 510 / 765) < 1e-6
    # 得分并列时帧号小的在前
    assert data["top"][0]["frame"] == FLASH_FRAME
    assert data["top"][1]["frame"] == FLASH_FRAME + 1


def test_point_mode_counter_pixel_scores(test_video: Path) -> None:
    result = detect_changes(test_video, point=COUNTER_POS)
    by_frame = {r.frame: r for r in result.records}
    # 计数像素每帧 (t*8,t*4,t*2)：常规相邻差 = 8+4+2 = 14
    assert by_frame[5].score == 14.0
    assert by_frame[5].previous_frame == 4
    # 闪白进入：|255-112|+|255-56|+|255-28| = 569
    assert by_frame[FLASH_FRAME].score == 569.0
    top = top_changes(result.records, 1)
    assert top[0].frame == FLASH_FRAME


def test_grid_mode(test_video: Path) -> None:
    data = run_json(
        "changes", test_video, "--grid", "0,0,32,32", "--step", 4,
        "--top", 1, "--json",
    )["data"]
    assert data["mode"] == "grid"
    assert data["grid"]["step"] == 4
    assert data["top"][0]["frame"] == FLASH_FRAME


def test_changes_csv_export(test_video: Path, tmp_path: Path) -> None:
    out_csv = tmp_path / "变化.csv"
    data = run_json(
        "changes", test_video, "--rect", "0,0,16,16",
        "--csv", out_csv, "--json",
    )["data"]
    assert data["csv_path"] == str(out_csv)
    with out_csv.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv_module.reader(fh))
    # 表头 + (T-1) 条相邻帧记录
    assert len(rows) == 1 + (FRAME_COUNT - 1)
    assert rows[0][0] == "frame"


def test_changes_range(test_video: Path) -> None:
    data = run_json(
        "changes", test_video, "--point", "24,16",
        "--start-frame", 0, "--end-frame", 10, "--json",
    )["data"]
    assert data["start_frame"] == 0 and data["end_frame"] == 10
    assert data["frames_analyzed"] == 11
    frames = {r["frame"] for r in data["top"]}
    assert frames <= set(range(1, 11))


def test_changes_needs_two_frames(test_video: Path) -> None:
    code, data = run_json_error(
        "changes", test_video, "--point", "0,0",
        "--start-frame", 5, "--end-frame", 5, "--json",
    )
    assert code == 2
    assert data["error"]["code"] == "INVALID_RANGE"


def test_changes_requires_exactly_one_target(test_video: Path) -> None:
    code, data = run_json_error(
        "changes", test_video, "--point", "0,0", "--rect", "0,0,4,4", "--json"
    )
    assert code == 2
