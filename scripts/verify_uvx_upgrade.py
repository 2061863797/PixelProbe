"""在临时 MCP 配置中演练固定 wheel 的升级和回滚，不改用户客户端配置。"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZipFile
from email.parser import Parser

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image


def identity(path):
    with ZipFile(path) as archive:
        metadata = archive.read(next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))).decode()
    parsed = Parser().parsestr(metadata)
    assert parsed["Name"] == "pixelprobe"
    return {"version": parsed["Version"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


async def verify(args):
    old, new = args.old.resolve(strict=True), args.new.resolve(strict=True)
    identities = [identity(old), identity(new)]
    assert identities[0]["version"] != identities[1]["version"], "升级必须使用两个不同真实版本"
    cache = args.cache.resolve()
    reports = []
    with tempfile.TemporaryDirectory(prefix="pixelprobe-upgrade-") as directory:
        root = Path(directory)
        source = root / "中文像素.png"
        Image.new("RGB", (1, 1), (17, 83, 201)).save(source)
        config = root / "mcp_config.json"

        def prefix(wheel):
            return [args.uvx, "--offline", "--cache-dir", str(cache), "--python", "3.12",
                    "--from", f"pixelprobe[mcp] @ {wheel.as_uri()}"]

        def warm(wheel, expected):
            completed = subprocess.run([*prefix(wheel), "pixelprobe", "--version"],
                                       capture_output=True, timeout=90, check=True)
            assert completed.stdout.decode("utf-8").strip() == f"pixelprobe {expected}"

        async def activate(wheel, expected, phase):
            selected = {"command": args.uvx, "args": [*prefix(wheel)[1:], "pixelprobe-mcp"]}
            config.write_text(json.dumps(selected), encoding="utf-8")
            selected = json.loads(config.read_text(encoding="utf-8"))
            env = {**os.environ, "PIXELPROBE_MCP_ROOTS": str(root),
                   "PIXELPROBE_MCP_ARTIFACT_ROOT": str(root / "artifacts")}
            async with stdio_client(StdioServerParameters(**selected, env=env)) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    # 1.x 用 pixelprobe_read_pixels；2.0 起改为 pixelprobe_measure（读取同一原生样本）
                    if "pixelprobe_read_pixels" in tools:
                        result = await session.call_tool("pixelprobe_read_pixels", {
                            "media_path": str(source), "points": [{"x": 0, "y": 0}],
                        })
                        assert not result.isError, result.content
                        sample = result.structuredContent["data"]["pixels"][0]["stored_sample"]
                    else:
                        result = await session.call_tool("pixelprobe_measure", {
                            "media_path": str(source), "what": "pixel", "at": "point(0,0)",
                            "options": {"sample": "native"},
                        })
                        assert not result.isError, result.content
                        sample = result.structuredContent["data"]["pixels"][0]["values"]
                    assert sample == [17, 83, 201], sample
            # stdio 上下文退出后才切换下一个版本，避免覆盖或复用运行中的进程。
            reports.append({"phase": phase, "version": expected, "mcp_pixel_verified": True,
                            "tool_count": len(tools)})

        warm(old, identities[0]["version"])
        await activate(old, identities[0]["version"], "before_upgrade")
        old_config = config.read_bytes()
        warm(new, identities[1]["version"])
        assert config.read_bytes() == old_config, "预安装不能提前切换客户端配置"
        await activate(new, identities[1]["version"], "after_upgrade")
        warm(old, identities[0]["version"])
        await activate(old, identities[0]["version"], "after_rollback")
        assert config.read_bytes() == old_config
    assert [identity(old), identity(new)] == identities, "升级不得修改原始 wheel"
    return {"offline": True, "python": "3.12", "artifacts": identities, "phases": reports,
            "scope": "temporary_config_local_wheels"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--uvx", default="uvx")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args)), ensure_ascii=True, indent=2))
