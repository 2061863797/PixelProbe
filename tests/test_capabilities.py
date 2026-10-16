"""能力登记表：命令行入口存在、结果编号可再次解析、CLI 与能力函数数值一致。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer.main

from conftest import FLASH_FRAME, run_json
from pixelprobe.address import parse_address
from pixelprobe.capabilities import all_capabilities, capability
from pixelprobe.cli.app import app
from pixelprobe.models.errors import InvalidRangeError


def _cli_commands() -> dict:
    return typer.main.get_command(app).commands  # type: ignore[attr-defined]


def test_every_capability_has_an_existing_cli_entry() -> None:
    commands = _cli_commands()
    for item in all_capabilities():
        words = item.cli.split()
        command = commands.get(words[0])
        assert command is not None, f"{item.name} 的命令行入口 {item.cli} 不存在"
        if len(words) > 1 and not words[1].startswith("--"):
            assert words[1] in command.commands, f"{item.name} 缺少子命令 {item.cli}"


def test_capability_names_and_mcp_entries_are_unique() -> None:
    items = all_capabilities()
    assert len({item.name for item in items}) == len(items)
    entries = [(item.mcp_tool, item.mcp_selector) for item in items]
    assert len(set(entries)) == len(entries), "两项能力不能映射到同一个 MCP 入口"


def test_invalid_options_are_reported_as_parameter_errors(test_video: Path) -> None:
    with pytest.raises(InvalidRangeError, match="changes 的参数无效"):
        capability("changes").invoke(test_video, parse_address(None), {"top": 0})
    with pytest.raises(InvalidRangeError, match="参数无效"):
        capability("changes").invoke(test_video, parse_address(None), {"unknown": 1})


def test_event_refs_resolve_back_to_the_event(test_video: Path, tmp_path: Path) -> None:
    payload = run_json("changes", test_video, "--at", "rect(0,0,32,32)", "--json")
    refs = payload["data"]["refs"]
    events = payload["data"]["events"]
    assert set(refs) == {f"event{n}" for n in range(1, len(events) + 1)}
    first = parse_address(refs["event1"])
    assert first.time is not None
    assert first.time.values[0] == events[0]["start_frame"]
    # 直接把这次的输出文件当作引用表，用 @event1 继续分析
    ref_file = tmp_path / "变化.json"
    ref_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    frame = run_json(
        "pixel", test_video, "--at", f"f={FLASH_FRAME} point(0,0)", "--ref-file", ref_file,
        "--json",
    )["data"]
    assert frame["frame"] == FLASH_FRAME
    region = run_json(
        "changes", test_video, "--at", "@event1 rect(0,0,8,8)", "--ref-file", ref_file, "--json",
    )["data"]
    assert region["start_frame"] == events[0]["start_frame"]
    assert region["end_frame"] == events[0]["end_frame"]


def test_cli_json_equals_capability_data(test_video: Path) -> None:
    """命令行输出 = 能力函数结果 + 命令行自己的输出路径字段。"""
    address = parse_address("f=15 rect(4,4,10,10)")
    direct = capability("region").invoke(test_video, address).data
    cli = run_json("region", test_video, "--at", str(address), "--json")["data"]
    cli.pop("crop_path")
    assert cli == json.loads(json.dumps(direct))
