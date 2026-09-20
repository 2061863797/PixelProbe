"""可选 MCP 依赖的友好启动入口。"""

from __future__ import annotations

import sys
import argparse
import os


def _load_server_main():
    """延迟导入 MCP 服务器，使可选依赖缺失时仍能给出稳定提示。"""
    from pixelprobe_mcp.server import main as run_server

    return run_server


def main() -> None:
    """启动服务器；未安装 MCP extra 时给出可执行的安装提示。"""
    parser = argparse.ArgumentParser(description="启动 PixelProbe MCP stdio 服务")
    parser.add_argument("--allow-root", action="append", help="允许的现有目录，可重复指定")
    parser.add_argument("--artifact-root", help="允许目录内的结果目录")
    parser.add_argument("--workers", type=int, help="并行媒体工作进程数，默认 2")
    parser.add_argument("--queue-size", type=int, help="等待队列容量，默认 16")
    options = parser.parse_args()
    from pixelprobe_mcp.config import ServerConfig
    try:
        config = ServerConfig.from_environment(
            allowed_roots=tuple(options.allow_root) if options.allow_root else None,
            artifact_root=options.artifact_root,
            workers=options.workers, queue_size=options.queue_size,
        )
    except (OSError, ValueError):
        parser.error("MCP 目录配置无效；请检查允许目录是否存在、结果目录是否位于允许范围")
    # 在延迟导入服务器前提供已校验的配置，也供后续子进程继承。
    if options.allow_root is not None:
        os.environ["PIXELPROBE_MCP_ROOTS"] = os.pathsep.join(map(str, config.allowed_roots))
    if options.artifact_root is not None:
        os.environ["PIXELPROBE_MCP_ARTIFACT_ROOT"] = str(config.artifact_root)
    if options.workers is not None:
        os.environ["PIXELPROBE_MCP_WORKERS"] = str(config.workers)
    if options.queue_size is not None:
        os.environ["PIXELPROBE_MCP_QUEUE_SIZE"] = str(config.queue_size)
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
