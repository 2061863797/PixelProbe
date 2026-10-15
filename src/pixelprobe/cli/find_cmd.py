"""pixelprobe find：用搜索语句定位视频中的时刻与区间。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import (
    AT_OPT,
    INDEX_DIR_OPT,
    JSON_OPT,
    NO_INDEX_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    REF_FILE_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    find_index,
    run_capability,
    with_refs,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_table, progress_bar


def find(
    media: Path = typer.Argument(..., help="视频路径"),
    query: str = typer.Argument(..., help=(
        '搜索语句，条件用 | 连接取并集，如 "black | flash"、"freeze >= 0.5s"、"gap"、'
        '"similar f=120"、"frame.luma > 200"、"rect(0,0,50,50).r < 30"、"first change in cell(B2)"'
    )),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    jump_threshold: float = typer.Option(
        0.1, "--jump-threshold", help="cut/flash 的跳变阈值：与上一帧的平均绝对差 / 255（0～1）",
    ),
    freeze_tolerance: float = typer.Option(
        0.1, "--freeze-tolerance", help="freeze：整帧平均绝对差不超过此值视为停住（0～255）",
    ),
    limit: int = typer.Option(200, "--limit", help="最多返回的结果区间数"),
    no_index: bool = NO_INDEX_OPT,
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """按条件搜索视频：黑帧/白帧/纯色帧、闪帧、镜头切换、重复帧、画面停住、掉帧间隔、
    相似帧、指标条件和区域变化。--at 可限定时间范围（如 "t=10s..60s"）。

    已建索引且只含整帧条件时直接查表；区域条件总是解码。结果是按固定数值定义判定的候选。
    """
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("find", ctx):
        with progress_bar("条件搜索", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "find", media, at, ref_file,
                {"query": query, "jump_threshold": jump_threshold,
                 "freeze_tolerance": freeze_tolerance, "limit": limit},
                progress=update,
                index=find_index(media, no_index=no_index, index_dir=index_dir),
            )
        data = with_refs(dict(outcome.data), outcome)
        if ctx.json_mode:
            json_writer.print_success("find", data)
            return
        if ctx.quiet:
            out_console.print(f"{data['match_count']} matches", highlight=False)
            return
        rows = [
            [f"@match{number}", item["kind"], item["start_frame"], item["end_frame"],
             f"{item['start_time']:.3f}", f"{item['end_time']:.3f}", item["value"]]
            for number, item in enumerate(data["matches"], start=1)
        ]
        title = (f"搜索结果：{data['match_count']} 个区间"
                 f"{'（已截断）' if data['truncated'] else ''}，分析 {data['frames_analyzed']} 帧"
                 f"（{'索引' if data['computed_from'] == 'index' else '解码'}）")
        print_table(title, ["编号", "条件", "起始帧", "结束帧", "起始秒", "结束秒", "值"], rows)
