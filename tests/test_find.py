"""find 搜索语言：解析、各条件的数值定义、索引与解码逐值一致。"""
from __future__ import annotations

import shutil
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

from pixelprobe.address import parse_address
from pixelprobe.capabilities import capability
from pixelprobe.find import parse_query
from pixelprobe.find.query import ChangeTerm, EventTerm, MetricTerm, SimilarTerm
from pixelprobe.index import IndexStore, build_index
from pixelprobe.models.errors import QueryError


def _run(media: Path, query: str, at: str | None = None, **context) -> dict:
    outcome = capability("find").invoke(media, parse_address(at), {"query": query}, **context)
    return {**outcome.data, "refs": outcome.refs}


def _write_video(path: Path, frames: list[np.ndarray], pts: list[int] | None = None) -> Path:
    """无损 ffv1 视频；pts 以毫秒计，缺省按 25 fps。"""
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width, stream.height = frames[0].shape[1], frames[0].shape[0]
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, 1000)
        for number, array in enumerate(frames):
            frame = av.VideoFrame.from_ndarray(array, format="rgb24")
            frame.time_base = Fraction(1, 1000)
            frame.pts = pts[number] if pts else number * 40
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def _solid(value: int | tuple[int, int, int], size: int = 32) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.uint8)


@pytest.fixture(scope="module")
def events_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """0-4 灰；5 白闪；6-9 灰；10 黑；11-19 切到蓝且画面停住；20-29 蓝底上移动的方块。"""
    gray, blue = _solid(120), _solid((20, 40, 200))
    frames = [gray] * 5 + [_solid(255)] + [gray] * 4 + [_solid(0)] + [blue] * 9
    for step in range(10):
        moving = blue.copy()
        moving[4:12, 2 + step * 2: 10 + step * 2] = (250, 250, 0)
        frames.append(moving)
    return _write_video(tmp_path_factory.mktemp("find") / "events.mkv", frames)


def test_parse_terms_and_normalized_text() -> None:
    terms = parse_query("Black | freeze >= 500ms | gap >= 2x | similar f=3 <= 4 | "
                        "frame.luma > 200 | CELL(B2).r < 30 | first change in rect(0,0,4,4) > 12")
    assert [type(term) for term in terms] == [
        EventTerm, EventTerm, EventTerm, SimilarTerm, MetricTerm, MetricTerm, ChangeTerm,
    ]
    assert [str(term) for term in terms] == [
        "black", "freeze >= 0.5s", "gap >= 2x", "similar f=3 <= 4", "frame.luma > 200",
        "cell(B2).r < 30", "first change in rect(0,0,4,4) > 12",
    ]


@pytest.mark.parametrize("query", [
    "", "unknown", "black >= 2", "freeze >= 3", "gap >= 0.5x", "similar rect(0,0,1,1)",
    "frame.sharp > 1", "rect(0,0,1,1).sharpness > 1", "change in row(3)", "frame.luma >",
    "similar f=1..3", "change in rect(0,0,2,2) > 300", "black | ",
])
def test_parse_rejects_invalid_queries(query: str) -> None:
    with pytest.raises(QueryError) as caught:
        parse_query(query)
    assert caught.value.code == "INVALID_QUERY" and caught.value.exit_code == 2


def test_frame_events_follow_documented_definitions(events_video: Path) -> None:
    data = _run(events_video, "black | white | flash | cut | duplicate | freeze >= 4f")
    found = {(item["kind"], item["start_frame"], item["end_frame"]) for item in data["matches"]}
    assert ("white", 5, 5) in found and ("flash", 5, 5) in found
    assert ("black", 10, 10) in found
    assert ("cut", 11, 11) in found          # 黑帧之后切到蓝色，一次跳变
    assert ("freeze", 11, 19) in found       # 11 首次出现，12-19 与上一帧相同
    assert ("duplicate", 12, 19) in found
    assert ("freeze", 0, 4) in found and ("freeze", 6, 9) in found
    assert all(item["kind"] != "freeze" or item["start_frame"] < 20 for item in data["matches"])
    freeze = next(item for item in data["matches"] if item["kind"] == "freeze" and item["start_frame"] == 11)
    assert freeze["duration_seconds"] == pytest.approx(0.36)  # 11 到 20 共 9 帧 × 40 ms
    assert data["refs"]["match1"].startswith("f=") and ".." in data["refs"]["match1"]
    assert set(data["definitions"]) == {"black", "white", "flash", "cut", "duplicate", "freeze"}


def test_seconds_threshold_and_time_range(events_video: Path) -> None:
    longer = _run(events_video, "freeze >= 0.3s")
    assert [(m["start_frame"], m["end_frame"]) for m in longer["matches"]] == [(11, 19)]
    ranged = _run(events_video, "black | white", at="f=6..29")
    assert [(m["kind"], m["start_frame"]) for m in ranged["matches"]] == [("black", 10)]
    assert ranged["start_frame"] == 6 and ranged["frames_analyzed"] == 24


def test_metric_similar_and_region_change(events_video: Path) -> None:
    luma = _run(events_video, "frame.luma > 200")
    assert [(m["start_frame"], m["end_frame"], m["value"]) for m in luma["matches"]] == [(5, 5, 255.0)]
    # 纯色画面的差分哈希都为 0（定义中已说明），带方块的画面不同
    similar = _run(events_video, "similar f=0 <= 0")
    assert [(m["start_frame"], m["end_frame"], m["reference_frame"]) for m in similar["matches"]] == [(0, 19, 0)]
    # 方块左边缘 x = 2 + 2s（s = 帧号 - 20），宽 8：第 28 帧盖住 rect 左半，第 29 帧盖满
    first = _run(events_video, "first change in rect(24,4,4,4) > 50", at="f=20..29")
    assert [m["start_frame"] for m in first["matches"]] == [28]
    region = _run(events_video, "rect(24,4,4,4).r > 200")
    assert [(m["start_frame"], m["end_frame"]) for m in region["matches"]] == [(5, 5), (29, 29)]


def test_gap_detects_dropped_frames(tmp_path: Path) -> None:
    pts = [0, 40, 80, 120, 240, 280, 320, 360, 400, 440]  # 120 → 240 缺了两帧
    frames = [_solid(10 * n) for n in range(10)]
    video = _write_video(tmp_path / "gap.mkv", frames, pts)
    data = _run(video, "gap")
    assert [(m["start_frame"], m["value"]) for m in data["matches"]] == [(4, 3.0)]
    assert data["matches"][0]["interval_seconds"] == pytest.approx(0.12)
    assert _run(video, "gap >= 4x")["match_count"] == 0


def test_index_and_decode_give_identical_matches(events_video: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy.mkv"
    shutil.copyfile(events_video, copy)
    index = IndexStore(tmp_path / "index").save(copy, build_index(copy))
    query = "black | white | flash | cut | duplicate | freeze >= 2f | gap | similar f=3 | frame.diff > 30"
    for at in (None, "f=4..25", "f=0..0"):
        decoded = _run(copy, query, at)
        indexed = _run(copy, query, at, index=index)
        assert decoded["computed_from"] == "decode" and indexed["computed_from"] == "index"
        assert decoded["matches"] == indexed["matches"], at
    # 含区域条件时即使有索引也必须解码
    assert _run(copy, "rect(0,0,4,4).luma > 100", index=index)["computed_from"] == "decode"


def test_limit_truncates_and_images_are_rejected(events_video: Path, test_image: Path) -> None:
    outcome = capability("find").invoke(events_video, parse_address(None), {"query": "similar f=0 <= 64", "limit": 1})
    assert outcome.data["match_count"] == 1
    from pixelprobe.models.errors import UnsupportedMediaError
    with pytest.raises(UnsupportedMediaError):
        _run(test_image, "black")


def test_cli_find_json_and_ref_file(events_video: Path, tmp_path: Path) -> None:
    from conftest import run_json, run_json_error
    data = run_json("find", str(events_video), "black | flash", "--json")["data"]
    assert [(m["kind"], m["start_frame"]) for m in data["matches"]] == [("flash", 5), ("black", 10)]
    refs = tmp_path / "refs.json"
    refs.write_text(__import__("json").dumps({"data": data}), encoding="utf-8")
    sheet = run_json("sheet", str(events_video), "--at", "@match2", "--ref-file", str(refs), "--json")
    assert sheet["data"]["frames"] == [10]
    code, error = run_json_error("find", str(events_video), "blak", "--json")
    assert code == 2 and error["error"]["code"] == "INVALID_QUERY"
