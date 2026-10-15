"""CLI 与 MCP 能力对等：每项登记的能力两侧都可用，且同一地址得到相同数值。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import pixelprobe_mcp.server as server
from pixelprobe.capabilities import all_capabilities
from pixelprobe.cli.app import app
from pixelprobe_mcp.config import ServerConfig
from pixelprobe_mcp.models import CapabilityInput
from pixelprobe_mcp.service import PixelProbeService

# 每个 MCP 工具用哪个参数选择具体能力；compare 由是否给出 reference_path 决定
SELECTOR_PARAMETER = {
    "pixelprobe_overview": "detail", "pixelprobe_look": "view", "pixelprobe_measure": "what",
    "pixelprobe_find": "kind", "pixelprobe_bundle": "action", "pixelprobe_index": "action",
    "pixelprobe_system": "action",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_every_capability_has_cli_and_mcp_entry() -> None:
    names = [item.name for item in all_capabilities()]
    assert len(names) == len(set(names))
    tools = server.mcp._tool_manager._tools  # FastMCP 已注册的工具
    runner = CliRunner()
    for item in all_capabilities():
        assert item.cli, f"{item.name} 缺少命令行入口"
        assert item.mcp_tool in tools, f"{item.name} 的 MCP 工具 {item.mcp_tool} 未注册"
        result = runner.invoke(app, [*item.cli.split(), "--help"])
        assert result.exit_code == 0, f"pixelprobe {item.cli} 不可用：{result.output}"
        parameter = SELECTOR_PARAMETER.get(item.mcp_tool)
        if parameter is not None:
            schema = tools[item.mcp_tool].parameters["properties"][parameter]
            assert item.mcp_selector in schema["enum"], f"{item.mcp_tool} 不接受 {parameter}={item.mcp_selector}"


@pytest.mark.anyio
async def test_every_mcp_selector_dispatches_to_its_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按登记表的 (工具, 选择值) 实际调用工具函数，确认落到同名能力上。"""
    seen: list[str] = []

    async def capture(capability: str, **_: object) -> str:
        seen.append(capability)
        return capability

    monkeypatch.setattr(server, "_capability", capture)
    for item in all_capabilities():
        tool = getattr(server, item.mcp_tool)
        media = {"media_path": "m.mkv"}
        if item.mcp_tool == "pixelprobe_compare":
            extra = {"reference_path": "r.mkv"} if item.mcp_selector == "media" else {}
            result = await tool(**media, **extra)
        elif item.mcp_tool == "pixelprobe_evidence":
            result = await tool(**media)
        elif item.mcp_tool == "pixelprobe_bundle":
            result = await tool(item.mcp_selector, media_path="m.mkv", bundle_path="b.bundle",
                                requests=[{}])
        elif item.mcp_selector == "query":
            result = await tool(**media, query="black")
        elif item.mcp_tool in {"pixelprobe_index", "pixelprobe_system"}:
            result = await tool(item.mcp_selector, **(media if item.mcp_tool == "pixelprobe_index" else {}))
        else:
            result = await tool(**media, **{SELECTOR_PARAMETER[item.mcp_tool]: item.mcp_selector})
        assert result == item.name, f"{item.mcp_tool}({item.mcp_selector}) 调用了 {result}"
    assert sorted(seen) == sorted(item.name for item in all_capabilities())


# ---------------- 数值一致性抽样 ----------------

_VOLATILE = {"images", "refs", "list_totals", "path", "computed_from", "bundle_path"}
# 只有命令行有的输出文件说明：保存位置与时间线图片的版式（MCP 的图片说明在 data.images）
_CLI_OUTPUT = {"orientation", "raw_width", "raw_height", "scale"}


def _normalized(data: dict) -> dict:
    clean = {
        key: value for key, value in data.items()
        if key not in _VOLATILE | _CLI_OUTPUT and not key.endswith("_path")
    }
    return json.loads(json.dumps(clean, ensure_ascii=False, default=str))


CASES = [
    ("info", "image", None, {}),
    ("pixel", "image", "point(3,4) point(0,0)", {}),
    ("region", "image", "rect(2,2,6,5)", {}),
    ("info", "video", None, {}),
    ("pixel", "video", "f=12 point(12,8)", {}),
    ("region", "video", "t=0.5s cell(B2)", {}),
    ("changes", "video", "f=0..29", {}),
    ("scan", "video", None, {}),
    ("timeline", "video", "f=0..20 point(1,1)", {}),
    ("spectrum", "blink", "point(1,1)", {}),
    ("spectrum2d", "image", None, {}),
    ("flow", "motion", "f=0,4", {}),
]
# find 的搜索语句是位置参数，单独比较
FIND_QUERY = "white | flash | freeze >= 2f | rect(0,0,4,4).r > 100 | first change in cell(B2)"


@pytest.fixture(scope="module")
def media(assets_dir: Path, blink_video: Path, motion_video: Path, test_image: Path,
          test_video: Path) -> dict[str, Path]:
    return {"image": test_image, "video": test_video, "blink": blink_video, "motion": motion_video}


@pytest.mark.parametrize(("name", "kind", "at", "options"), CASES,
                         ids=[f"{case[0]}-{case[1]}" for case in CASES])
def test_cli_and_mcp_return_identical_values(
    media: dict[str, Path], assets_dir: Path, name: str, kind: str, at: str | None, options: dict,
) -> None:
    from conftest import run_json
    source = media[kind]
    args = [name, str(source), "--json"]
    if at:
        args[2:2] = ["--at", at]
    cli = run_json(*args)["data"]
    service = PixelProbeService(ServerConfig((assets_dir,), assets_dir / "parity-artifacts"))
    envelope, _ = service.run_capability(CapabilityInput(
        capability=name, media_path=str(source), at=at, options=options, images=False, limit=200,
    ))
    mcp = envelope.data
    shared = set(_normalized(cli)) & set(_normalized(mcp))
    assert shared, "两侧没有共同字段"
    # MCP 只做加法（图片说明、索引状态等），不得改变或丢失命令行的数值字段
    assert set(_normalized(cli)) <= set(_normalized(mcp)), set(_normalized(cli)) - set(_normalized(mcp))
    for key in shared:
        assert _normalized(cli)[key] == _normalized(mcp)[key], key


def test_cli_and_mcp_find_return_identical_matches(test_video: Path, assets_dir: Path) -> None:
    from conftest import run_json
    cli = run_json("find", str(test_video), FIND_QUERY, "--json")["data"]
    service = PixelProbeService(ServerConfig((assets_dir,), assets_dir / "parity-artifacts"))
    envelope, _ = service.run_capability(CapabilityInput(
        capability="find", media_path=str(test_video), options={"query": FIND_QUERY}, limit=200,
    ))
    assert _normalized(cli) == _normalized(envelope.data)
    assert cli["refs"] == envelope.data["refs"]
