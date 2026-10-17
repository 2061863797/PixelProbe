"""Claude Code / Codex 插件清单：版本、启动命令、Skill 与市场条目保持一致。"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "pixelprobe"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _servers() -> dict[str, dict]:
    return {
        ".mcp.json": _json(PLUGIN / ".mcp.json")["mcpServers"],
        "mcp.json": _json(PLUGIN / "mcp.json")["mcpServers"],
        "codex-mcp.json": _json(PLUGIN / "codex-mcp.json")["mcpServers"],
    }


def test_manifests_share_name_version_and_metadata() -> None:
    manifests = [_json(PLUGIN / ".claude-plugin" / "plugin.json"), _json(PLUGIN / "plugin.json"),
                 _json(PLUGIN / ".codex-plugin" / "plugin.json")]
    assert {m["name"] for m in manifests} == {"pixelprobe"}
    assert len({m["version"] for m in manifests}) == 1
    assert {m["license"] for m in manifests} == {"Apache-2.0"}
    assert len({m["description"] for m in manifests}) == 1


def test_every_server_launches_the_same_pinned_release() -> None:
    version = _json(PLUGIN / "plugin.json")["version"]
    for name, servers in _servers().items():
        assert len(servers) == 1, name
        server = next(iter(servers.values()))
        assert server["command"] == "uvx", name
        source = server["args"][server["args"].index("--from") + 1]
        assert source.endswith(f".git@v{version}"), (name, source)
        # 回归：插件只装 [mcp] 时环境里没有 OpenCV、Zarr、C2PA，光流与取证 Skill 必然失败
        assert source.startswith("pixelprobe[mcp,flow,storage,evidence] @ git+https://github.com/"), name
        assert server["args"][-1] == "pixelprobe-mcp", name
    # Claude Code 按插件自动加命名空间；Codex 不加，服务名避开用户手动配置的 "pixelprobe"
    assert set(_servers()[".mcp.json"]) == {"pixelprobe"}
    assert set(_servers()["mcp.json"]) == set(_servers()["codex-mcp.json"]) == {"pixelprobe-plugin"}
    assert _servers()["mcp.json"]["pixelprobe-plugin"]["type"] == "stdio"
    roots = _servers()[".mcp.json"]["pixelprobe"]["env"]["PIXELPROBE_MCP_ROOTS"]
    assert roots == "${user_config.media_roots}"
    assert "media_roots" in _json(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]
    memory = _servers()[".mcp.json"]["pixelprobe"]["env"]["PIXELPROBE_MCP_TOTAL_MEMORY_MB"]
    assert memory == "${user_config.total_memory_mb}"
    assert "total_memory_mb" in _json(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]


def test_skills_have_matching_frontmatter_and_only_name_existing_tools() -> None:
    tools = {
        "pixelprobe_overview", "pixelprobe_look", "pixelprobe_measure", "pixelprobe_find",
        "pixelprobe_compare", "pixelprobe_evidence", "pixelprobe_bundle", "pixelprobe_index",
        "pixelprobe_job", "pixelprobe_system",
    }
    skills = sorted(path for path in (PLUGIN / "skills").iterdir() if path.is_dir())
    assert [path.name for path in skills] == [
        "forensics", "long-video", "pixelprobe-core", "quality", "ui-recording",
    ]
    for directory in skills:
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\nname: (?P<name>[^\n]+)\ndescription: (?P<desc>[^\n]+)\n---\n", text)
        assert match, directory.name
        assert match["name"] == directory.name
        assert 50 <= len(match["desc"]) <= 1024
        mentioned = set(re.findall(r"pixelprobe_[a-z_]+", text))
        assert mentioned <= tools, (directory.name, mentioned - tools)


def test_marketplaces_point_at_the_plugin() -> None:
    claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
    codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    assert claude["plugins"][0]["source"] == "./plugins/pixelprobe"
    entry = codex["plugins"][0]
    assert entry["source"] == {"source": "local", "path": "./plugins/pixelprobe"}
    assert entry["policy"]["installation"] == "AVAILABLE" and entry["category"]
    assert claude["name"] == codex["name"] == "pixelprobe"
