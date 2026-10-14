"""pixelprobe index：建立、查看、删除和清理媒体索引。

索引是可删除的本机加速数据：一次顺序解码，之后 changes（整帧、逐帧）与 scan（逐帧采样）
可直接查表，结果与解码逐值相同；媒体文件改变后旧索引自动失效。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import (
    INDEX_DIR_OPT,
    JSON_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar

app = typer.Typer(help="建立和管理媒体索引（可安全删除的本机加速数据）", no_args_is_help=True)


def _store(index_dir: Optional[Path]):
    from pixelprobe.index import IndexStore
    return IndexStore(index_dir)


def _report(command: str, ctx: CliContext, data: dict, title: str) -> None:
    if ctx.json_mode:
        json_writer.print_success(command, data)
    elif ctx.quiet:
        out_console.print(str(data.get("status", "")), highlight=False)
    else:
        print_kv(title, list(data.items()))


@app.command("build")
def build(
    media: Path = typer.Argument(..., help="视频路径"),
    force: bool = typer.Option(False, "--force", help="已有有效索引时也重新建立"),
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """顺序解码一遍，生成元数据、每帧体检表、分段和缩略立方体。"""
    from pixelprobe.index import build_index
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("index build", ctx):
        store = _store(index_dir)
        existing = None if force else store.lookup(media)
        if existing is not None:
            data = {"status": "existing", **existing.summary(), "index_dir": str(store.root)}
        else:
            with progress_bar("建立索引", 1, ctx.progress_disabled) as update:
                built = build_index(media, progress=update)
            index = store.save(media, built)
            data = {"status": "built", **index.summary(), "index_dir": str(store.root)}
        _report("index build", ctx, data, "媒体索引")


@app.command("show")
def show(
    media: Path = typer.Argument(..., help="视频路径"),
    segments: bool = typer.Option(False, "--segments", help="同时列出全部分段"),
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """查看媒体当前内容对应的索引；没有或已失效时报告 missing。"""
    ctx = CliContext(json_mode, quiet, verbose, True)
    with cli_guard("index show", ctx):
        store = _store(index_dir)
        index = store.lookup(media)
        if index is None:
            data: dict = {"status": "missing", "index_dir": str(store.root)}
        else:
            data = {"status": "ready", **index.summary(), "index_dir": str(store.root)}
            if segments:
                data["segments"] = index.segments
        _report("index show", ctx, data, "媒体索引")


@app.command("remove")
def remove(
    media: Path = typer.Argument(..., help="视频路径"),
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """删除该媒体当前内容对应的索引。"""
    ctx = CliContext(json_mode, quiet, verbose, True)
    with cli_guard("index remove", ctx):
        store = _store(index_dir)
        removed = store.remove(media)
        _report("index remove", ctx, {
            "status": "removed" if removed else "missing", "index_dir": str(store.root),
        }, "媒体索引")


@app.command("prune")
def prune(
    older_than_days: float = typer.Option(
        30, "--older-than-days", min=0, help="至少多少天未使用",
    ),
    all_entries: bool = typer.Option(False, "--all", help="不论时间，选中全部索引"),
    apply: bool = typer.Option(False, "--apply", help="执行删除；默认只预览"),
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """清理长时间未使用的索引；默认只预览，加 --apply 才删除。"""
    ctx = CliContext(json_mode, quiet, verbose, True)
    with cli_guard("index prune", ctx):
        report = _store(index_dir).prune(
            max_age_seconds=None if all_entries else older_than_days * 86400,
            dry_run=not apply,
        )
        report["status"] = "pruned" if apply else "preview"
        _report("index prune", ctx, report, "索引清理")
