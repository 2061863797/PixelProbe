"""核心 CLI 与可选 MCP 适配层的发行边界契约。"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest
import typer

from conftest import run_cli
from pixelprobe.cli import CliContext, cli_guard

ROOT = Path(__file__).resolve().parent.parent


def test_distribution_keeps_mcp_optional_and_separate() -> None:
    """核心 CLI 不强制依赖 MCP；适配层使用独立入口和可选 extra。"""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    setuptools = config["tool"]["setuptools"]

    assert project["scripts"] == {
        "pixelprobe": "pixelprobe.cli.app:main",
        "pixelprobe-mcp": "pixelprobe_mcp.entry:main",
    }
    assert all(
        not dependency.lower().startswith("mcp")
        for dependency in project["dependencies"]
    )
    assert config["project"]["optional-dependencies"]["mcp"] == [
        "mcp>=1.27,<2"
    ]
    assert "package-data" not in setuptools


def test_removed_in_core_interfaces_are_not_shipped() -> None:
    """旧的内嵌 MCP、Web 服务与 GUI 仍不得混回 pixelprobe 核心包。"""
    assert importlib.util.find_spec("pixelprobe.mcp_server") is None
    assert importlib.util.find_spec("pixelprobe.webapp") is None
    assert importlib.util.find_spec("pixelprobe_mcp") is not None
    assert not (ROOT / "src" / "pixelprobe" / "static").exists()


@pytest.mark.parametrize(
    "arguments",
    (("info", "--json"), ("info", "--json", "--not-an-option")),
)
def test_json_mode_typer_usage_errors_are_machine_readable(
    arguments: tuple[str, ...],
) -> None:
    result = run_cli(*arguments)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["success"] is False
    assert payload["command"] == "info"
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
    assert payload["error"]["message"]
    assert result.stderr == ""


def test_json_mode_cancellation_keeps_stdout_machine_readable(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(typer.Exit) as raised:
        with cli_guard("info", CliContext(json_mode=True)):
            raise KeyboardInterrupt
    assert raised.value.exit_code == 130
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "success": False,
        "command": "info",
        "error": {
            "code": "CANCELLED",
            "message": "操作已取消，临时文件已清理",
        },
    }
    assert captured.err == ""


def test_ci_checks_tests_wheel_and_standalone_cli_before_release() -> None:
    """常规提交必须在打标签前覆盖测试、wheel 安装和三平台 CLI。"""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8",
    )

    assert "pull_request:" in workflow
    assert "branches:\n      - main" in workflow
    assert "python-version: [\"3.11\", \"3.12\", \"3.13\"]" in workflow
    assert "python -m pytest" in workflow
    assert "pip install dist/*.whl" in workflow
    assert all(
        runner in workflow
        for runner in ("windows-latest", "ubuntu-latest", "macos-14")
    )


def test_release_archives_executable_at_archive_root() -> None:
    """安装文档按解压根目录运行，发布配置必须维持相同布局。"""
    workflow = (ROOT / ".github" / "workflows" / "release-cli.yml").read_text(
        encoding="utf-8",
    )

    assert "Compress-Archive -Path '${{ matrix.archive_name }}\\*'" in workflow
    assert 'tar -czf "${{ matrix.archive }}" -C "${{ matrix.archive_name }}" .' in workflow
    assert "archive-smoke" in workflow
