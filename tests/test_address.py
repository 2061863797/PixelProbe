"""统一地址写法：解析、规范化、错误定位、引用展开与换算。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FLASH_FRAME, make_frame, run_json, run_json_error
from pixelprobe.address import (
    Cell,
    Grid,
    Path as PathShape,
    PixelId,
    Point,
    Rect,
    Row,
    TimeSpec,
    cell_rect,
    check,
    pair_options,
    parse_address,
    points_of,
    range_options,
    rect_of,
    single_frame_options,
)
from pixelprobe.models.errors import AddressError, InvalidRangeError


@pytest.mark.parametrize(
    "text,expected",
    (
        ("f=7", TimeSpec("frame", "single", (7,))),
        ("f=3..25", TimeSpec("frame", "range", (3, 25))),
        ("f=3..", TimeSpec("frame", "range", (3, None))),
        ("f=..25", TimeSpec("frame", "range", (None, 25))),
        ("f=0,5", TimeSpec("frame", "list", (0, 5))),
        ("f=all", TimeSpec("frame", "all")),
        ("t=0.5s", TimeSpec("time", "single", (0.5,))),
        ("t=0.5", TimeSpec("time", "single", (0.5,))),
        ("t=500ms", TimeSpec("time", "single", (0.5,))),
        ("t=1:02.5", TimeSpec("time", "single", (62.5,))),
        ("t=1:00:00", TimeSpec("time", "single", (3600.0,))),
        ("t=1s..3s", TimeSpec("time", "range", (1.0, 3.0))),
        ("t=0s,0.5s", TimeSpec("time", "list", (0.0, 0.5))),
    ),
)
def test_time_forms(text: str, expected: TimeSpec) -> None:
    assert parse_address(text).time == expected


def test_shapes_parse_in_written_order() -> None:
    address = parse_address(
        "point(1,2) pid(40) rect(3,4,5,6) grid(0,0,32,32 step=4) row(7) "
        "cell(B2:C3, 4x4) line(0,1 -> 31,1) path(0,0 -> 1.5,2 -> 3,3)"
    )
    assert address.shapes == (
        Point(1, 2), PixelId(40), Rect(3, 4, 5, 6), Grid(0, 0, 32, 32, 4), Row(7),
        Cell(1, 1, 2, 2, 4, 4),
        PathShape(((0.0, 1.0), (31.0, 1.0)), kind="line"),
        PathShape(((0.0, 0.0), (1.5, 2.0), (3.0, 3.0)), kind="path"),
    )


def test_order_case_and_spacing_are_free_and_canonical_form_round_trips() -> None:
    address = parse_address("  RECT(1,2,3,4)   step=2  F=10..20 ")
    assert str(address) == "f=10..20 step=2 rect(1,2,3,4)"
    assert parse_address(str(address)) == address


def test_cell_layout_is_canonicalised() -> None:
    assert str(parse_address("cell(c3:b2)")) == "cell(B2:C3)"
    assert str(parse_address("cell(A1, 8x6)")) == "cell(A1)"


def test_empty_address_is_allowed() -> None:
    assert parse_address(None).is_empty
    assert parse_address("   ").is_empty


@pytest.mark.parametrize(
    "text,fragment",
    (
        ("f=-1", "非负整数"),
        ("f=5..2", "结束早于开始"),
        ("f=..", "至少要给出一端"),
        ("f=1,,2", "空项"),
        ("f=", "缺少值"),
        ("t=abc", "时间格式无法识别"),
        ("t=1:75", "小于 60"),
        ("f=1 t=2s", "时间被指定了两次"),
        ("step=0", "正整数"),
        ("step=1 step=2", "step 被指定了两次"),
        ("rect(1,2,3)", "需要 4 个整数"),
        ("rect(0,0,0,4)", "宽高必须 >= 1"),
        ("point(1,2", "缺少右括号"),
        ("foo(1)", "未知形状"),
        ("zzz=1", "未知项"),
        ("cell(Z9)", "超出 8x6 网格"),
        ("cell(A1, 0x3)", "列数应为"),
        ("line(0,0 -> 1,1 -> 2,2)", "恰好需要两个点"),
        ("path(1,1)", "至少需要两个点"),
        ("grid(0,0,4,4 step=x)", "正整数"),
        ("plain", "后应为 = 或 ("),
        ("#", "无法识别"),
    ),
)
def test_errors_name_the_problem(text: str, fragment: str) -> None:
    with pytest.raises(AddressError) as excinfo:
        parse_address(text)
    assert fragment in excinfo.value.message
    assert "第" in excinfo.value.message  # 给出出错位置
    assert excinfo.value.hint  # 附带写法示例
    assert isinstance(excinfo.value, InvalidRangeError)
    assert excinfo.value.exit_code == 2


def test_error_position_points_at_offending_item() -> None:
    with pytest.raises(AddressError, match="第 5 个字符处"):
        parse_address("f=1 bogus(2)")


def test_references_expand_and_merge() -> None:
    refs = {"event3": "f=10..14", "box": "rect(1,2,3,4)", "both": "@event3 @box"}
    assert str(parse_address("@event3 rect(0,0,8,8)", refs)) == "f=10..14 rect(0,0,8,8)"
    assert str(parse_address("@both step=2", refs)) == "f=10..14 step=2 rect(1,2,3,4)"


def test_reference_errors() -> None:
    with pytest.raises(AddressError, match="找不到引用 @missing"):
        parse_address("@missing", {"other": "f=1"})
    with pytest.raises(AddressError, match="时间被指定了两次"):
        parse_address("@a f=2", {"a": "f=1"})
    with pytest.raises(AddressError, match="嵌套过深"):
        parse_address("@loop", {"loop": "@loop"})


def test_cell_rect_tiles_the_frame_without_gaps() -> None:
    width, height = 1919, 1081
    covered_x: list[int] = []
    for column in range(8):
        x, _, w, _ = cell_rect(Cell(column, 0, column, 0), width, height)
        covered_x.extend(range(x, x + w))
    covered_y: list[int] = []
    for row in range(6):
        _, y, _, h = cell_rect(Cell(0, row, 0, row), width, height)
        covered_y.extend(range(y, y + h))
    assert covered_x == list(range(width))
    assert covered_y == list(range(height))
    assert cell_rect(Cell(7, 5, 7, 5), 1920, 1080) == (1680, 900, 240, 180)
    assert cell_rect(Cell(1, 1, 2, 2, 4, 4), 32, 32) == (8, 8, 16, 16)


def test_cell_grid_larger_than_frame_is_rejected() -> None:
    with pytest.raises(AddressError, match="大于画面"):
        cell_rect(Cell(0, 0, 0, 0), 4, 4)


def test_conversions() -> None:
    assert range_options(parse_address("f=3..25 step=2")) == {
        "start_frame": 3, "end_frame": 25, "start": None, "end": None, "sample_every": 2,
    }
    assert range_options(parse_address("t=1s..")) == {
        "start_frame": None, "end_frame": None, "start": 1.0, "end": None, "sample_every": 1,
    }
    assert range_options(parse_address("f=7"))["start_frame"] == 7
    assert range_options(parse_address(""))["start_frame"] is None
    assert single_frame_options(parse_address("t=0.5s")) == {"frame": None, "time": 0.5}
    assert pair_options(parse_address("f=0,5"))["frame_b"] == 5
    assert points_of(parse_address("pid(19) point(1,2)"), 16, 16) == [(3, 1), (1, 2)]
    assert rect_of(parse_address("cell(A1)"), 32, 32) == (0, 0, 4, 5)
    with pytest.raises(AddressError, match="连续范围"):
        range_options(parse_address("f=0,5"))
    with pytest.raises(AddressError, match="恰好两帧"):
        pair_options(parse_address("f=0"))


def test_check_explains_what_the_command_accepts() -> None:
    address = parse_address("f=0..5 point(1,1)")
    with pytest.raises(AddressError) as excinfo:
        check(address, command="frame", time_modes={"single"}, shapes={"rect"})
    assert "frame 的地址" in excinfo.value.message and "单帧" in excinfo.value.message
    with pytest.raises(AddressError, match="不能同时指定点和区域"):
        check(
            parse_address("point(1,1) rect(0,0,2,2)"), command="x",
            shapes={"point", "rect"},
        )
    with pytest.raises(AddressError, match="最多只能有 1 个区域"):
        check(parse_address("rect(0,0,2,2) cell(A1)"), command="x", shapes={"rect", "cell"})
    with pytest.raises(AddressError, match="不接受 step="):
        check(parse_address("step=2"), command="x")


def test_cli_ref_file_accepts_envelope_and_flat_mapping(
    test_video: Path, tmp_path: Path,
) -> None:
    envelope = tmp_path / "refs.json"
    envelope.write_text(json.dumps({
        "success": True, "data": {"refs": {"flash": f"f={FLASH_FRAME}"}},
    }), encoding="utf-8")
    data = run_json(
        "pixel", test_video, "--at", "@flash point(0,0)", "--ref-file", envelope, "--json",
    )["data"]
    assert data["frame"] == FLASH_FRAME
    assert data["pixels"][0]["rgb"] == {"r": 255, "g": 255, "b": 255}

    flat = tmp_path / "flat.json"
    flat.write_text(json.dumps({"pt": "point(12,8)"}), encoding="utf-8")
    data = run_json(
        "pixel", test_video, "--at", "f=12 @pt", "--ref-file", flat, "--json",
    )["data"]
    r, g, b = make_frame(12)[8, 12]
    assert data["pixels"][0]["rgb"] == {"r": int(r), "g": int(g), "b": int(b)}


def test_cli_reports_bad_ref_file_and_bad_address(test_video: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    code, data = run_json_error(
        "frame", test_video, "--at", "@x", "--ref-file", bad, "--json",
    )
    assert code == 2 and data["error"]["code"] == "INVALID_ADDRESS"
    code, data = run_json_error("frame", test_video, "--at", "f=1 frame(2)", "--json")
    assert code == 2 and data["error"]["code"] == "INVALID_ADDRESS"
    assert "hint" in data["error"]
