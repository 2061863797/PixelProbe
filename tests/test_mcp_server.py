"""pixelprobe-mcp 测试：进程内内存传输，验证工具注册、调用与错误处理。"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from PIL import Image

pytest.importorskip("mcp")

from mcp.shared.memory import (
    create_connected_server_and_client_session as client_session,
)

from conftest import FLASH_FRAME, FRAME_COUNT, GREEN_POS
from pixelprobe.mcp_server import SERVER_INSTRUCTIONS, mcp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _call(name: str, arguments: dict):
    async with client_session(mcp._mcp_server) as client:
        return await client.call_tool(name, arguments)


def _text(result) -> str:
    return next(c.text for c in result.content if c.type == "text")


def test_server_instructions_prioritize_native_vision() -> None:
    instructions = mcp._mcp_server.create_initialization_options().instructions
    assert instructions == SERVER_INSTRUCTIONS
    assert "原生视觉/视频理解" in instructions
    assert "精确数据辅助工具" in instructions
    assert "不能单独证明" in instructions
    assert "发生前、候选帧和发生后的画面" in instructions


def test_server_instructions_include_scenario_map() -> None:
    """场景速查表引导"按问题选工具"，且覆盖各分析入口。"""
    instructions = mcp._mcp_server.create_initialization_options().instructions
    assert "场景速查" in instructions
    for tool in (
        "scan_media", "sample_frames", "detect_changes", "compare_frames",
        "temporal_reduce", "temporal_spectrum", "spatial_spectrum",
        "optical_flow", "extract_timeline", "inspect_pixels",
    ):
        assert tool in instructions, tool


async def test_scan_media_description_routes_next_steps() -> None:
    """scan_media 描述自含后续路标（不依赖客户端支持 instructions）。"""
    async with client_session(mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    description = by_name["pixelprobe_scan_media"].description or ""
    assert "下一步" in description
    for tool in ("compare_frames", "temporal_reduce", "temporal_spectrum",
                 "optical_flow"):
        assert tool in description, tool


async def test_all_tools_registered() -> None:
    async with client_session(mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
    names = {t.name for t in tools}
    assert names == {
        "pixelprobe_get_media_info",
        "pixelprobe_extract_frame",
        "pixelprobe_inspect_pixels",
        "pixelprobe_analyze_region",
        "pixelprobe_extract_timeline",
        "pixelprobe_xt_slice",
        "pixelprobe_yt_slice",
        "pixelprobe_detect_changes",
        "pixelprobe_temporal_reduce",
        "pixelprobe_compare_frames",
        "pixelprobe_sample_frames",
        "pixelprobe_scan_media",
        "pixelprobe_temporal_spectrum",
        "pixelprobe_spatial_spectrum",
        "pixelprobe_optical_flow",
        "pixelprobe_save_frame",
        "pixelprobe_save_timeline",
        "pixelprobe_save_xt_slice",
        "pixelprobe_save_yt_slice",
        "pixelprobe_save_temporal_reduce",
    }


async def test_numeric_tools_describe_their_auxiliary_role() -> None:
    async with client_session(mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    timeline = by_name["pixelprobe_extract_timeline"].description or ""
    changes = by_name["pixelprobe_detect_changes"].description or ""
    assert "辅助视觉理解" in timeline
    assert "不能单独证明" in timeline
    assert "辅助筛选" in changes
    assert "不能单独证明" in changes


async def test_tool_annotations_match_side_effects() -> None:
    async with client_session(mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    save_names = {
        "pixelprobe_save_frame",
        "pixelprobe_save_timeline",
        "pixelprobe_save_xt_slice",
        "pixelprobe_save_yt_slice",
        "pixelprobe_save_temporal_reduce",
    }
    for name, tool in by_name.items():
        assert tool.annotations is not None
        if name in save_names:
            assert tool.annotations.readOnlyHint is False
            assert tool.annotations.destructiveHint is True
        else:
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False


async def test_get_media_info(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_get_media_info", {"path": str(test_video)}
    )
    assert not result.isError
    data = json.loads(_text(result))
    assert data["media_type"] == "video"
    assert data["width"] == 32 and data["frame_count"] == FRAME_COUNT


async def test_extract_frame_returns_image(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_extract_frame",
        {"path": str(test_video), "frame": FLASH_FRAME},
    )
    assert not result.isError
    types = [c.type for c in result.content]
    assert "text" in types and "image" in types
    meta = json.loads(_text(result))
    assert meta["frame"] == FLASH_FRAME
    assert "saved_path" not in meta
    image = next(c for c in result.content if c.type == "image")
    assert image.mimeType == "image/png"
    assert len(image.data) > 0


async def test_inspect_pixels(test_video: Path) -> None:
    gx, gy = GREEN_POS
    result = await _call(
        "pixelprobe_inspect_pixels",
        {"path": str(test_video), "frame": 3, "points": [f"{gx},{gy}"]},
    )
    data = json.loads(_text(result))
    assert data["pixels"][0]["rgb"] == {"r": 0, "g": 255, "b": 0}


async def test_detect_changes_finds_flash(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_detect_changes",
        {"path": str(test_video), "rect": "0,0,32,32", "top": 1},
    )
    data = json.loads(_text(result))
    assert data["top"][0]["frame"] == FLASH_FRAME


async def test_detect_changes_default_full_with_events(
    test_video: Path,
) -> None:
    result = await _call(
        "pixelprobe_detect_changes", {"path": str(test_video)}
    )
    data = json.loads(_text(result))
    assert data["mode"] == "full"
    assert len(data["events"]) == 1
    event = data["events"][0]
    assert event["start_frame"] <= FLASH_FRAME <= event["end_frame"]
    assert data["event_threshold_used"] > 0


async def test_detect_changes_curve_options(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_detect_changes",
        {
            "path": str(test_video),
            "include_curve": True,
            "include_curve_image": True,
        },
    )
    data = json.loads(_text(result))
    assert len(data["curve"]) == FRAME_COUNT - 1
    assert data["curve"][0][0] == 1  # [frame, normalized_score]
    assert any(c.type == "image" for c in result.content)


async def test_temporal_reduce_returns_stats_and_image(
    test_video: Path,
) -> None:
    result = await _call(
        "pixelprobe_temporal_reduce",
        {"path": str(test_video), "op": "max", "rect": "24,16,1,1"},
    )
    assert not result.isError
    meta = json.loads(_text(result))
    assert meta["op"] == "max"
    assert meta["stat_max"] == [255.0, 255.0, 255.0]
    assert meta["frames_analyzed"] == FRAME_COUNT
    assert any(c.type == "image" for c in result.content)


async def test_compare_frames_bbox(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_compare_frames",
        {"path": str(test_video), "frame_a": 0, "frame_b": 5},
    )
    meta = json.loads(_text(result))
    assert meta["changed_pixels"] == 3
    assert meta["bbox"]["x"] == 0
    assert any(c.type == "image" for c in result.content)


async def test_sample_frames_grid(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_sample_frames",
        {"path": str(test_video), "count": 4},
    )
    meta = json.loads(_text(result))
    assert len(meta["frames"]) == 4
    assert meta["frames"][0] == 0
    assert meta["frames"][-1] == FRAME_COUNT - 1
    assert meta["cols"] == 2 and meta["rows"] == 2
    assert any(c.type == "image" for c in result.content)


async def test_save_temporal_reduce_writes_png(
    test_video: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "统计图.png"
    result = await _call(
        "pixelprobe_save_temporal_reduce",
        {"path": str(test_video), "op": "std", "output_path": str(output)},
    )
    assert not result.isError
    assert json.loads(_text(result))["saved_path"] == str(output)
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.size == (32, 32)


async def test_scan_media_overview(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_scan_media", {"path": str(test_video), "sheet_count": 4}
    )
    assert not result.isError
    meta = json.loads(_text(result))
    assert meta["info"]["frame_count"] == FRAME_COUNT
    assert len(meta["sheet_frames"]) == 4
    assert len(meta["events"]) == 1
    images = [c for c in result.content if c.type == "image"]
    assert len(images) == 2  # 网格图 + 变化曲线


async def test_scan_media_single_frame(tmp_path: Path) -> None:
    """单帧视频：不生成变化曲线但正常返回网格图，不报错。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from generate_test_video import _encode_lossless_verified, make_frame

    video = _encode_lossless_verified(
        tmp_path / "单帧.mkv", [make_frame(0)], 30, "单帧"
    )
    result = await _call(
        "pixelprobe_scan_media", {"path": str(video), "sheet_count": 3}
    )
    assert not result.isError
    meta = json.loads(_text(result))
    assert meta["events"] == []
    images = [c for c in result.content if c.type == "image"]
    assert len(images) == 1  # 仅网格图


async def test_temporal_spectrum_tool(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_temporal_spectrum",
        {"path": str(test_video), "source": "change"},
    )
    assert not result.isError
    meta = json.loads(_text(result))
    assert meta["samples"] == FRAME_COUNT - 1
    assert any(c.type == "image" for c in result.content)


async def test_optical_flow_missing_dependency(
    test_video: Path, monkeypatch,
) -> None:
    """无 cv2 环境：工具照常注册，调用返回 DEPENDENCY_MISSING 与安装提示。"""
    import pixelprobe.core.optical_flow as of
    from pixelprobe.models.errors import DependencyMissingError

    def missing():
        raise DependencyMissingError(
            "光流分析需要 OpenCV，但当前环境未安装",
            hint='pip install "pixelprobe[flow]"',
        )

    monkeypatch.setattr(of, "require_cv2", missing)
    result = await _call(
        "pixelprobe_optical_flow",
        {"path": str(test_video), "frame_a": 0, "frame_b": 1},
    )
    text = _text(result)
    assert text.startswith("Error[DEPENDENCY_MISSING]")
    assert "pixelprobe[flow]" in text


async def test_timeline_with_values(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_extract_timeline",
        {
            "path": str(test_video),
            "points": ["24,16"],
            "include_values": True,
        },
    )
    meta = json.loads(_text(result))
    assert meta["k_points"] == 1 and meta["t_frames"] == FRAME_COUNT
    assert meta["values"][0][0] == [0, 255, 0]
    assert meta["values"][0][FLASH_FRAME] == [255, 255, 255]
    assert any(c.type == "image" for c in result.content)


async def test_xt_slice_metadata(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_xt_slice", {"path": str(test_video), "coordinate": 8}
    )
    meta = json.loads(_text(result))
    assert meta["slice_type"] == "xt"
    assert meta["raw_width"] == 32 and meta["raw_height"] == FRAME_COUNT
    assert meta["display_scale"] >= 1


async def test_tool_schemas_are_flat() -> None:
    """参数必须直接暴露在 schema 顶层，不允许嵌套 params 对象（防回归）。"""
    async with client_session(mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
    for tool in tools:
        properties = tool.inputSchema.get("properties", {})
        assert "params" not in properties, tool.name
        assert "path" in properties, tool.name


async def test_invalid_argument_value_is_rejected(test_video: Path) -> None:
    """已知参数的取值约束（如 frame >= 0）仍然生效。"""
    result = await _call(
        "pixelprobe_extract_frame", {"path": str(test_video), "frame": -1}
    )
    assert result.isError


async def test_read_tool_ignores_output_path(
    test_video: Path, tmp_path: Path,
) -> None:
    """只读工具收到未知参数 output_path 时忽略之，绝不写盘。"""
    output = tmp_path / "不应写入.png"
    result = await _call(
        "pixelprobe_extract_frame",
        {
            "path": str(test_video),
            "frame": 0,
            "output_path": str(output),
        },
    )
    assert not result.isError
    assert not output.exists()


async def test_save_tools_write_png_files(
    test_video: Path, tmp_path: Path,
) -> None:
    cases = [
        (
            "pixelprobe_save_frame",
            {"path": str(test_video), "frame": FLASH_FRAME},
            tmp_path / "帧.png",
        ),
        (
            "pixelprobe_save_timeline",
            {"path": str(test_video), "points": ["24,16"]},
            tmp_path / "时间线.png",
        ),
        (
            "pixelprobe_save_xt_slice",
            {"path": str(test_video), "coordinate": 8},
            tmp_path / "xt.png",
        ),
        (
            "pixelprobe_save_yt_slice",
            {"path": str(test_video), "coordinate": 2},
            tmp_path / "yt.png",
        ),
    ]
    for name, params, output in cases:
        output.write_bytes(b"old")
        result = await _call(
            name, {**params, "output_path": str(output)}
        )
        assert not result.isError
        assert json.loads(_text(result))["saved_path"] == str(output)
        with Image.open(output) as image:
            assert image.format == "PNG"


async def test_error_is_actionable(test_video: Path) -> None:
    result = await _call(
        "pixelprobe_inspect_pixels",
        {"path": str(test_video), "points": ["999,999"]},
    )
    text = _text(result)
    assert text.startswith("Error[COORDINATE_OUT_OF_RANGE]")
    assert "0～31" in text  # 错误信息包含有效范围，可指导下一步


async def test_missing_file_error() -> None:
    result = await _call(
        "pixelprobe_get_media_info", {"path": "不存在的视频.mp4"}
    )
    assert _text(result).startswith("Error[FILE_NOT_FOUND]")
