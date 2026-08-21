"""Rich 终端输出（人类可读模式）与进度条。

约定：结果表格走 stdout；日志、警告、进度条一律走 stderr，
保证 --json 模式下 stdout 纯净。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table


def _configure_output_encoding() -> None:
    """输出编码无法表示中文界面时切换到 UTF-8。"""
    probe = "PixelProbe：本地图片与视频像素分析"
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not encoding or not callable(reconfigure):
            continue
        try:
            probe.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            reconfigure(encoding="utf-8", errors="strict")


_configure_output_encoding()
out_console = Console()
err_console = Console(stderr=True)


def print_kv(title: str, rows: list[tuple[str, object]]) -> None:
    """打印键值表。"""
    table = Table(title=title, show_header=False)
    table.add_column("字段", style="cyan")
    table.add_column("值")
    for key, value in rows:
        table.add_row(key, "" if value is None else str(value))
    out_console.print(table)


def print_table(title: str, headers: list[str], rows: list[list[object]]) -> None:
    """打印通用表格。"""
    table = Table(title=title)
    for header in headers:
        table.add_column(header)
    for row in rows:
        table.add_row(*("" if v is None else str(v) for v in row))
    out_console.print(table)


@contextmanager
def progress_bar(
    description: str, total: int, disabled: bool
) -> Iterator:
    """产出一个 update(done, total) 回调；disabled 时为空操作。

    进度条渲染到 stderr，不污染 stdout。
    """
    if disabled:
        yield lambda done, total_: None
        return
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=err_console,
        transient=True,
    )
    with progress:
        task_id = progress.add_task(description, total=total)
        def update(done: int, total_: int) -> None:
            progress.update(task_id, completed=done, total=total_)
        yield update
