"""可选 MCP 依赖的友好启动入口。"""

from __future__ import annotations

import sys


def _load_server_main():
    """延迟导入 MCP 服务器，使可选依赖缺失时仍能给出稳定提示。"""
    from pixelprobe_mcp.server import main as run_server

    return run_server


def main() -> None:
    """启动服务器；未安装 MCP extra 时给出可执行的安装提示。"""
    try:
        run_server = _load_server_main()
    except ModuleNotFoundError as exc:
        if exc.name in {"mcp", "anyio"}:
            print(
                "PixelProbe MCP 可选依赖未完整安装（需要 mcp 和 anyio）。请执行："
                "python -m pip install 'pixelprobe[mcp]'",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        raise
    run_server()


if __name__ == "__main__":
    main()
