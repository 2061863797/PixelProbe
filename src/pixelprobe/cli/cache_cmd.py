"""pixelprobe cache：管理与正式 Bundle 隔离的本机缓存。"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from pixelprobe.cli import JSON_OPT, CliContext, cli_guard
from pixelprobe.engine.cache import LocalArrayCache
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console

app = typer.Typer(help="管理可安全删除的本机执行缓存", no_args_is_help=True)


def default_cache_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "PixelProbe" / "cache"


@app.command("clear")
def clear(
    cache_dir: Path | None = typer.Option(None, "--cache-dir", help="自定义缓存目录"),
    json_mode: bool = JSON_OPT,
) -> None:
    """删除缓存条目；不会查找或删除任何 Bundle。"""
    ctx = CliContext(json_mode=json_mode, no_progress=True)
    with cli_guard("cache clear", ctx):
        root = (cache_dir or default_cache_root()).resolve()
        removed = LocalArrayCache(root).clear()
        data = {"cache_dir": str(root), "removed_entries": removed}
        if json_mode:
            json_writer.print_success("cache clear", data)
        else:
            out_console.print(f"已清理 {removed} 个缓存条目：{root}", highlight=False)
