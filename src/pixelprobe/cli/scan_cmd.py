"""pixelprobe scan：一键概览扫描（信息 + 网格图 + 变化事件 + 异常帧）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe import core
from pixelprobe.cli import (
    JSON_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png
from pixelprobe.output.plot import render_curve


def scan(
    media: Path = typer.Argument(..., help="视频路径"),
    sheet_count: int = typer.Option(9, "--sheet-count", help="概览网格抽帧数"),
    sample_every: Optional[int] = typer.Option(
        None, "--sample-every",
        help="每隔 N 帧采样一次（缺省自动：全片约 1800 帧封顶）",
    ),
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
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """未知视频的第一步：单遍解码产出概览网格、变化事件与异常帧。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("scan", ctx):
        with progress_bar("媒体扫描", 1, ctx.progress_disabled) as update:
            result = core.scan_media(
                media,
                sheet_count=sheet_count,
                sample_every=sample_every,
                event_threshold=threshold,
                progress=update,
            )
        if sheet_output is not None:
            save_png(result.sheet.image, sheet_output)
        curve_saved: str | None = None
        if curve_output is not None:
            if result.records:
                curve = render_curve(
                    [r.normalized_score for r in result.records], y_min=0.0
                )
                save_png(curve, curve_output)
                curve_saved = str(curve_output)
            elif not ctx.json_mode and not ctx.quiet:
                out_console.print(
                    "帧数不足两帧，未生成变化曲线", highlight=False
                )

        data = {
            "info": result.info.model_dump(),
            "effective_sample_every": result.effective_sample_every,
            "frames_analyzed": result.frames_analyzed,
            "sheet_frames": result.sheet.frames,
            "events": [e.to_dict() for e in result.events],
            "event_threshold_used": result.event_threshold,
            "anomalies": result.anomalies,
            "anomalies_truncated": result.anomalies_truncated,
            "sheet_output_path": str(sheet_output) if sheet_output else None,
            "curve_output_path": curve_saved,
        }
        if ctx.json_mode:
            json_writer.print_success("scan", data)
            return
        if ctx.quiet:
            out_console.print(
                f"{len(result.events)} events {len(result.anomalies)} anomalies",
                highlight=False,
            )
            return
        info = result.info
        rows: list[tuple[str, object]] = [
            ("媒体", f"{info.width}x{info.height} {info.codec}"),
            ("分析帧数",
             f"{result.frames_analyzed}（每 {result.effective_sample_every} 帧）"),
            ("事件数", len(result.events)),
            ("异常帧",
             f"{len(result.anomalies)}"
             f"{'（已截断）' if result.anomalies_truncated else ''}"),
        ]
        if sheet_output:
            rows.append(("网格图", str(sheet_output)))
        if curve_saved:
            rows.append(("变化曲线", curve_saved))
        print_kv("媒体扫描", rows)
