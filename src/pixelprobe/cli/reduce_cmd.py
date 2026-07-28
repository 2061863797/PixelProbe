"""pixelprobe reduce：时间域合成，把整段视频折叠为一张逐像素统计图。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

import pixelprobe
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
from pixelprobe.compat.legacy_requests import legacy_reduce_request


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
        request = legacy_reduce_request(
            media,
            operation=op,
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
        )
        with progress_bar("时间域合成", 1, ctx.progress_disabled) as update:
            generated = pixelprobe.generate(request)
            update(1, 1)
        data_tensor, preview_tensor = generated.request_tensors[0]
        statistic = data_tensor.data.materialize()
        image = preview_tensor.data.materialize()
        frames = tuple(data_tensor.attributes["presentation_indices"])
        preview_attributes = preview_tensor.attributes
        if output is not None:
            save_png(image, output)

        data = {
            "op": op,
            "rect": (
                {"x": rect_tuple[0], "y": rect_tuple[1],
                 "width": rect_tuple[2], "height": rect_tuple[3]}
                if rect_tuple else None
            ),
            "start_frame": frames[0],
            "end_frame": frames[-1],
            "sample_every": request.selection.sample_every,
            "frames_analyzed": len(frames),
            "stat_min": [round(float(value), 4) for value in statistic.min(axis=(0, 1))],
            "stat_max": [round(float(value), 4) for value in statistic.max(axis=(0, 1))],
            "stat_mean": [round(float(value), 4) for value in statistic.mean(axis=(0, 1))],
            "stretch_low_value": preview_attributes["stretch_low_value"],
            "stretch_high_value": preview_attributes["stretch_high_value"],
            "stretch_domain": preview_attributes["stretch_domain"],
            "p_low": p_low,
            "p_high": p_high,
            "destripe": destripe,
            "smooth": smooth,
            "image_width": int(image.shape[1]),
            "image_height": int(image.shape[0]),
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("reduce", data)
            return
        if ctx.quiet:
            out_console.print(
                str(output) if output else f"op={op}", highlight=False
            )
            return
        rows: list[tuple[str, object]] = [
            ("统计量", op),
            ("分析帧数", len(frames)),
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
