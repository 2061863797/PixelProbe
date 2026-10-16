"""配置优先级、权限诊断及服务发现可用性。"""
import json
import sys

import pytest

from conftest import run_cli
from pixelprobe_mcp.config import ServerConfig, PathAccessError


def test_explicit_roots_override_environment_without_expanding_access(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXELPROBE_MCP_ROOTS", str(tmp_path / "missing"))
    config = ServerConfig.from_environment(allowed_roots=(str(tmp_path),))
    assert config.allowed_roots == (tmp_path.resolve(),)
    with pytest.raises(PathAccessError):
        ServerConfig.from_environment(
            allowed_roots=(str(tmp_path),), artifact_root=str(tmp_path.parent),
        )


def test_doctor_invalid_config_returns_json_and_failure(tmp_path):
    result = run_cli("doctor", "--json", "--allow-root", str(tmp_path / "missing"))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["healthy"] is False
    assert {"name": "mcp_config", "status": "error"} in payload["data"]["checks"]


def test_doctor_checks_writable_parent_without_creating_output(tmp_path):
    from pixelprobe.cli.doctor_cmd import inspect_environment
    output = tmp_path / "new" / "artifacts"
    result = inspect_environment((str(tmp_path),), str(output))
    storage = next(item for item in result["checks"] if item["name"] == "artifact_storage")
    assert storage["status"] == "ok"
    assert storage["free_bytes"] > 0
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_unset_roots_prefer_claude_project_dir_then_cwd(tmp_path, monkeypatch):
    """插件把留空的 media_roots 传成空字符串；此时用 Claude Code 给出的项目根目录。"""
    project, launch = tmp_path / "项目", tmp_path / "启动目录"
    project.mkdir()
    launch.mkdir()
    monkeypatch.chdir(launch)
    monkeypatch.setenv("PIXELPROBE_MCP_ROOTS", "  ")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert ServerConfig.from_environment().allowed_roots == (project.resolve(),)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert ServerConfig.from_environment().allowed_roots == (launch.resolve(),)
    monkeypatch.setenv("PIXELPROBE_MCP_ROOTS", str(project))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(launch))
    assert ServerConfig.from_environment().allowed_roots == (project.resolve(),)


def test_mcp_arguments_are_applied_before_server_import(tmp_path, monkeypatch):
    import pixelprobe_mcp.entry as entry
    monkeypatch.setenv("PIXELPROBE_MCP_ROOTS", "missing")
    monkeypatch.setenv("PIXELPROBE_MCP_ARTIFACT_ROOT", "missing")
    monkeypatch.setattr(sys, "argv", ["pixelprobe-mcp", "--allow-root", str(tmp_path),
                                     "--artifact-root", str(tmp_path / "output")])
    seen = []
    def load():
        seen.append(ServerConfig.from_environment())
        return lambda: None
    monkeypatch.setattr(entry, "_load_server_main", load)
    entry.main()
    assert seen[0].allowed_roots == (tmp_path.resolve(),)


@pytest.mark.anyio
async def test_capabilities_remain_available_while_media_slot_busy():
    import pixelprobe_mcp.server as server
    result = await server.pixelprobe_system("capabilities")
    assert result.structuredContent["data"]["server"] == "pixelprobe_mcp"


@pytest.fixture
def anyio_backend():
    return "asyncio"
