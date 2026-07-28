"""可选 MCP 启动入口的无依赖行为测试。"""

from __future__ import annotations

import pytest

import pixelprobe_mcp.entry as entry_module


def test_mcp_entry_reports_missing_mcp_extra(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未安装 mcp extra 时，入口应给出安装命令而不是导入栈。"""

    def missing_server() -> object:
        raise ModuleNotFoundError("No module named 'mcp'", name="mcp")

    monkeypatch.setattr(entry_module, "_load_server_main", missing_server)

    with pytest.raises(SystemExit) as exited:
        entry_module.main()

    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pixelprobe[mcp]" in captured.err
    assert "可选依赖未完整安装" in captured.err
