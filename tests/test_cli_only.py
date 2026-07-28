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
