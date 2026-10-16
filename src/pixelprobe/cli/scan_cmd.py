"""pixelprobe scan：一键概览扫描（信息 + 网格图 + 变化事件 + 异常帧）。"""

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
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png


def scan(
    media: Path = typer.Argument(..., help="视频路径"),
    sheet_count: int = typer.Option(9, "--sheet-count", help="概览网格抽帧数"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    threshold: Optional[float] = typer.Option(
        None, "--threshold",
        help="事件分段阈值（缺省自动取剔除最大记录后的 mean + 3*std）",
    ),
    sheet_output: Optional[Path] = typer.Option(
        None, "--sheet-output", help="概览网格图 PNG 输出路径"
    ),
    curve_output: Optional[Path] = typer.Option(
        None, "--curve-output", help="变化曲线图 PNG 输出路径"
    ),
    no_index: bool = NO_INDEX_OPT,
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """未知视频的第一步：单遍解码产出概览网格、变化事件与异常帧。

    扫描全片；--at "step=N" 可指定每隔 N 帧采样，缺省自动（全片约 1800 帧封顶）。
    """
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("scan", ctx):
        with progress_bar("媒体扫描", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "scan", media, at, ref_file,
                {"sheet_count": sheet_count, "threshold": threshold},
                progress=update,
                index=find_index(media, no_index=no_index, index_dir=index_dir),
            )
        data = dict(outcome.data)
        if sheet_output is not None:
            save_png(outcome.images["sheet"](), sheet_output)
        curve_saved: str | None = None
        if curve_output is not None:
            if "curve" in outcome.images:
                save_png(outcome.images["curve"](), curve_output)
                curve_saved = str(curve_output)
            elif not ctx.json_mode and not ctx.quiet:
                out_console.print("帧数不足两帧，未生成变化曲线", highlight=False)
        data.update(
            sheet_output_path=str(sheet_output) if sheet_output else None,
            curve_output_path=curve_saved,
        )
        with_refs(data, outcome)
        if ctx.json_mode:
            json_writer.print_success("scan", data)
            return
        if ctx.quiet:
            out_console.print(
                f"{len(data['events'])} events {len(data['anomalies'])} anomalies",
                highlight=False,
            )
            return
        info = data["info"]
        rows: list[tuple[str, object]] = [
            ("媒体", f"{info['width']}x{info['height']} {info['codec']}"),
            ("分析帧数",
             f"{data['frames_analyzed']}（每 {data['effective_sample_every']} 帧）"),
            ("事件数", len(data["events"])),
            ("异常帧",
             f"{len(data['anomalies'])}"
             f"{'（已截断）' if data['anomalies_truncated'] else ''}"),
        ]
        if sheet_output:
            rows.append(("网格图", str(sheet_output)))
        if curve_saved:
            rows.append(("变化曲线", curve_saved))
        print_kv("媒体扫描", rows)
