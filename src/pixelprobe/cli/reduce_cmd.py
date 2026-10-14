"""pixelprobe reduce：时间域合成，把整段视频折叠为一张逐像素统计图。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import (
    AT_OPT,
    JSON_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    REF_FILE_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    run_capability,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png


def reduce(
    media: Path = typer.Argument(..., help="视频路径"),
    op: str = typer.Option(
        "std", "--op",
        help="统计量：mean/median/min/max/std/diff（diff=相邻帧差均值）",
    ),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
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
    """逐像素时间统计（噪声藏图、慢变水印、运动能量用 --op std/diff）：--at "t=1s..5s step=2 rect(...)"。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("reduce", ctx):
        with progress_bar("时间域合成", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "reduce", media, at, ref_file,
                {"op": op, "p_low": p_low, "p_high": p_high,
                 "destripe": destripe, "smooth": smooth},
                progress=update,
            )
        data = dict(outcome.data)
        if output is not None:
            save_png(outcome.images["reduce"](), output)
        data["output_path"] = str(output) if output else None
        if ctx.json_mode:
            json_writer.print_success("reduce", data)
            return
        if ctx.quiet:
            out_console.print(str(output) if output else f"op={op}", highlight=False)
            return
        rows: list[tuple[str, object]] = [
            ("统计量", op),
            ("分析帧数", data["frames_analyzed"]),
            ("原始统计范围",
             f"{min(data['stat_min'])} ~ {max(data['stat_max'])}"),
            ("拉伸端点",
             f"{data['stretch_low_value']} ~ {data['stretch_high_value']}"
             f"（P{p_low} ~ P{p_high}"
             + ("" if data["stretch_domain"] == "raw"
                else f"，空间：{data['stretch_domain']}") + "）"),
        ]
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("时间域合成", rows)
