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
from pixelprobe.mcp_server import mcp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _call(name: str, arguments: dict):
    async with client_session(mcp._mcp_server) as client:
        return await client.call_tool(name, {"params": arguments})


def _text(result) -> str:
    return next(c.text for c in result.content if c.type == "text")


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
        "pixelprobe_save_frame",
        "pixelprobe_save_timeline",
        "pixelprobe_save_xt_slice",
        "pixelprobe_save_yt_slice",
    }


async def test_tool_annotations_match_side_effects() -> None:
    async with client_session(mcp._mcp_server) as client:
        tools = (await client.list_tools()).tools
    by_name = {tool.name: tool for tool in tools}
    save_names = {
        "pixelprobe_save_frame",
        "pixelprobe_save_timeline",
        "pixelprobe_save_xt_slice",
        "pixelprobe_save_yt_slice",
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


async def test_read_tool_rejects_output_path(
    test_video: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "不应写入.png"
    result = await _call(
        "pixelprobe_extract_frame",
        {
            "path": str(test_video),
            "frame": 0,
            "output_path": str(output),
        },
    )
    assert result.isError
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
