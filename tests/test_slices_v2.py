"""2.0 新切法：差分切片与自适应时间轴。"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from pixelprobe.address import parse_address
from pixelprobe.capabilities import capability
from pixelprobe.index import IndexStore, build_index
from pixelprobe.models.errors import AddressError
from test_find import events_video  # noqa: F401  （模块级素材：灰/白闪/黑/蓝/移动方块）


def _slice(media: Path, kind: str, at: str, **options):
    outcome = capability(kind).invoke(media, parse_address(at), options, **options.pop("_context", {}))
    return outcome.data, outcome.images["slice"]()


def test_diff_slice_equals_difference_of_plain_slice(events_video: Path) -> None:
    plain, plain_image = _slice(events_video, "xt", "row(8)")
    diff, diff_image = _slice(events_video, "xt", "row(8)", source="diff")
    assert diff["frames"] == plain["frames"][1:]            # 第 0 帧没有前帧
    expected = np.abs(np.diff(plain_image.astype(np.int16), axis=0)).astype(np.uint8)
    np.testing.assert_array_equal(diff_image, expected)
    row5 = diff["frames"].index(5)
    assert (diff_image[row5] == 135).all()                  # 灰 120 → 白 255
    assert diff["source"] == "diff" and "value_definition" in diff
    # 带 step 时与"上一展示帧"相比，而不是与上一采样行相比
    stepped, stepped_image = _slice(events_video, "yt", "f=4..12 step=4 col(3)", source="diff")
    assert stepped["frames"] == [4, 8, 12]
    assert (stepped_image[0] == 0).all() and (stepped_image[1] == 0).all()


def test_adaptive_axis_concentrates_rows_on_changes(events_video: Path, tmp_path: Path) -> None:
    data, image = _slice(events_video, "xt", "row(8)", time_axis="adaptive", rows=10)
    frames = data["frames"]
    assert frames[0] == 0 and frames[-1] == 29 and len(frames) == 10   # 行数用满
    assert image.shape[0] == len(frames) and data["sample_every"] is None
    assert "time_axis_note" in data
    # 0-4 / 6-9 / 12-19 静止段只分到很少的行；闪帧、黑帧、切换和移动方块段分到多数
    busy = sum(1 for frame in frames if frame in {5, 6, 10, 11} or frame >= 20)
    assert busy >= len(frames) / 2
    # 索引与解码给出相同的取帧
    copy = tmp_path / "copy.mkv"
    shutil.copyfile(events_video, copy)
    index = IndexStore(tmp_path / "index").save(copy, build_index(copy))
    indexed, _ = _slice(copy, "xt", "row(8)", time_axis="adaptive", rows=10, _context={"index": index})
    assert indexed["frames"] == frames
    with pytest.raises(AddressError):
        _slice(events_video, "xt", "step=2 row(8)", time_axis="adaptive")
    short, _ = _slice(events_video, "xt", "f=0..5 row(8)", time_axis="adaptive", rows=50)
    assert short["frames"] == [0, 1, 2, 3, 4, 5]


def test_cli_slice_options(events_video: Path) -> None:
    from conftest import run_json
    data = run_json("xt", str(events_video), "--at", "row(8)", "--source", "diff",
                    "--time-axis", "adaptive", "--rows", "6", "--json")["data"]
    assert data["source"] == "diff" and data["time_axis_mode"] == "adaptive"
    assert data["sample_every"] is None and len(data["frames"]) == 6
    # 单帧剧烈变化（白闪）不会吞掉行数预算
    from pixelprobe.capabilities.temporal import _adaptive_frames
    spike = np.zeros(30)
    spike[15] = 1000.0
    picked = _adaptive_frames(spike, 0, 12)
    assert len(picked) == 12 and 15 in picked and picked[0] == 0 and picked[-1] == 29
