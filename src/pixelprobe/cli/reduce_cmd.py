"""pixelprobe reduce：时间域合成，把整段视频折叠为一张逐像素统计图。"""

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
from pixelprobe.utils.coordinates import parse_rect


def reduce(
    media: Path = typer.Argument(..., help="视频路径"),
    op: str = typer.Option(
        "std", "--op",
        help="统计量：mean/median/min/max/std/diff（diff=相邻帧差均值）",
    ),
    rect: Optional[str] = typer.Option(
        None, "--rect", help="只统计子区域 x,y,width,height"
    ),
    start_frame: Optional[int] = typer.Option(
        None, "--start-frame", help="起始帧（含）"
    ),
    end_frame: Optional[int] = typer.Option(
        None, "--end-frame", help="结束帧（含）"
    ),
    start: Optional[float] = typer.Option(None, "--start", help="起始时间（秒）"),
    end: Optional[float] = typer.Option(None, "--end", help="结束时间（秒）"),
    sample_every: int = typer.Option(1, "--sample-every", help="每隔 N 帧采样一次"),
    p_low: float = typer.Option(
        1.0, "--p-low", help="对比度拉伸低百分位（0-100）"
    ),
    p_high: float = typer.Option(
        99.0, "--p-high", help="对比度拉伸高百分位（0-100）"
    ),
    destripe: bool = typer.Option(
        False, "--destripe", help="扣除逐列/逐行均值，抑制条纹伪影"
    ),
    smooth: int = typer.Option(
        0, "--smooth", help="N×N 邻域均值平滑（>=2 生效），压制噪声粒度"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="统计图 PNG 输出路径；不指定则只输出统计摘要"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """逐像素时间统计（噪声藏图、慢变水印、运动能量等场景用 --op std/diff）。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("reduce", ctx):
        rect_tuple = parse_rect(rect) if rect else None
        with progress_bar("时间域合成", 1, ctx.progress_disabled) as update:
            result = core.temporal_reduce(
                media,
                op=op,  # type: ignore[arg-type]
                rect=rect_tuple,
                start_frame=start_frame,
                end_frame=end_frame,
                start=start,
                end=end,
                sample_every=sample_every,
                p_low=p_low,
                p_high=p_high,
                destripe=destripe,
                smooth=smooth,
                progress=update,
            )
        if output is not None:
            save_png(result.image, output)

        data = {
            "op": result.op,
            "rect": (
                {"x": rect_tuple[0], "y": rect_tuple[1],
                 "width": rect_tuple[2], "height": rect_tuple[3]}
                if rect_tuple else None
            ),
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "sample_every": result.frame_range.sample_every,
            "frames_analyzed": result.frames_analyzed,
            "stat_min": result.stat_min,
            "stat_max": result.stat_max,
            "stat_mean": result.stat_mean,
            "stretch_low_value": result.stretch_low_value,
            "stretch_high_value": result.stretch_high_value,
            "p_low": result.p_low,
            "p_high": result.p_high,
            "destripe": result.destripe,
            "smooth": result.smooth,
            "image_width": int(result.image.shape[1]),
            "image_height": int(result.image.shape[0]),
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("reduce", data)
            return
        if ctx.quiet:
            out_console.print(
                str(output) if output else f"op={result.op}", highlight=False
            )
            return
        rows: list[tuple[str, object]] = [
            ("统计量", result.op),
            ("分析帧数", result.frames_analyzed),
            ("原始统计范围",
             f"{min(result.stat_min)} ~ {max(result.stat_max)}"),
            ("拉伸端点",
             f"{result.stretch_low_value} ~ {result.stretch_high_value}"
             f"（P{result.p_low} ~ P{result.p_high}）"),
        ]
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("时间域合成", rows)
