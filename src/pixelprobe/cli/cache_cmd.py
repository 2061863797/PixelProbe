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


@app.command("inspect")
def inspect_cache(
    cache_dir: Path | None = typer.Option(None, "--cache-dir", help="自定义缓存目录"),
    json_mode: bool = JSON_OPT,
) -> None:
    """预览可确认归属的缓存占用，不删除数据。"""
    ctx = CliContext(json_mode=json_mode, no_progress=True)
    with cli_guard("cache inspect", ctx):
        report = LocalArrayCache(cache_dir or default_cache_root()).maintain()
        if json_mode:
            json_writer.print_success("cache inspect", report)
        else:
            out_console.print(report)


@app.command("prune")
def prune_cache(
    cache_dir: Path | None = typer.Option(None, "--cache-dir", help="自定义缓存目录"),
    older_than_days: float = typer.Option(7, "--older-than-days", min=0, help="至少多少天未修改"),
    apply: bool = typer.Option(False, "--apply", help="执行删除；默认只预览"),
    json_mode: bool = JSON_OPT,
) -> None:
    """清理过期且无活动引用的缓存，保留无法确认归属的文件。"""
    ctx = CliContext(json_mode=json_mode, no_progress=True)
    with cli_guard("cache prune", ctx):
        report = LocalArrayCache(cache_dir or default_cache_root()).maintain(
            dry_run=not apply, max_age_seconds=older_than_days * 86400,
        )
        if json_mode:
            json_writer.print_success("cache prune", report)
        else:
            out_console.print(report)


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
