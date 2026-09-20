"""诊断依赖与 MCP 目录配置，不启动服务或创建结果目录。"""

from __future__ import annotations

import importlib
import os
import shutil
import tempfile

import typer

from pixelprobe.cli import JSON_OPT
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console
from pixelprobe_mcp.config import ServerConfig


def inspect_environment(
    allowed_roots: tuple[str, ...] | None = None,
    artifact_root: str | None = None,
) -> dict:
    checks: list[dict] = []
    for name in ("av", "numpy", "PIL", "psutil", "mcp", "cv2", "zarr", "c2pa"):
        try:
            module = importlib.import_module(name)
            checks.append({
                "name": name, "status": "ok",
                "version": str(getattr(module, "__version__", "unknown")),
            })
        except Exception:
            # 缺失可选功能不阻止核心 CLI，但必须显示而非默默跳过。
            checks.append({"name": name, "status": "unavailable"})
    try:
        config = ServerConfig.from_environment(
            allowed_roots=allowed_roots, artifact_root=artifact_root,
        )
        readable = all(os.access(root, os.R_OK | os.X_OK) for root in config.allowed_roots)
        checks.append({"name": "allowed_roots", "status": "ok" if readable else "error"})
        parent = config.artifact_root
        while not parent.exists():
            parent = parent.parent
        free_bytes = shutil.disk_usage(parent).free
        try:
            # 实际创建并立即删除探针；os.access 在 Windows ACL 下不足以验证写权限。
            with tempfile.TemporaryFile(dir=parent) as probe:
                probe.write(b"pixelprobe")
                probe.flush()
            writable = True
        except OSError:
            writable = False
        checks.append({
            "name": "artifact_storage", "status": "ok" if writable else "error",
            "free_bytes": free_bytes,
        })
    except (OSError, ValueError):
        checks.append({"name": "mcp_config", "status": "error"})
    required = {"av", "numpy", "PIL", "psutil", "mcp_config", "allowed_roots", "artifact_storage"}
    return {
        "healthy": all(item["status"] == "ok" for item in checks if item["name"] in required),
        "checks": checks,
        "limitations": ["依赖导入不等于实际媒体解码验证", "可用磁盘空间不等于任务配额保障"],
    }


def doctor(
    allow_root: list[str] | None = typer.Option(None, "--allow-root", help="允许目录，可重复指定"),
    artifact_root: str | None = typer.Option(None, "--artifact-root", help="结果目录"),
    json_mode: bool = JSON_OPT,
) -> None:
    """检查运行环境、可选能力、MCP 配置和结果目录写入权限。"""
    result = inspect_environment(tuple(allow_root) if allow_root else None, artifact_root)
    if json_mode:
        json_writer.print_success("doctor", result)
    else:
        for check in result["checks"]:
            out_console.print(f"{check['name']}: {check['status']}", highlight=False)
    if not result["healthy"]:
        raise typer.Exit(1)
