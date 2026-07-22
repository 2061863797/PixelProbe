"""pixelprobe xt / yt：时空切片命令。

- xt：固定 y 的水平扫描线，横轴=原视频 x，纵轴=时间；
- yt：固定 x 的垂直扫描线，横轴=原视频 y，纵轴=时间。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

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
from pixelprobe.models.spacetime import SpacetimeMetadata
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png, scale_nearest
from pixelprobe.utils.validation import ensure_scale


def _run_slice(
    slice_type: Literal["xt", "yt"],
    media: Path,
    fixed: int,
    start_frame: Optional[int],
    end_frame: Optional[int],
    start: Optional[float],
    end: Optional[float],
    sample_every: int,
    scale_space: int,
    scale_t: int,
    output: Optional[Path],
    ctx: CliContext,
) -> None:
    with cli_guard(slice_type, ctx):
        ensure_scale(scale_space, "--scale-x" if slice_type == "xt" else "--scale-y")
        ensure_scale(scale_t, "--scale-t")
        desc = "生成 X–T 切片" if slice_type == "xt" else "生成 Y–T 切片"
        with progress_bar(desc, 1, ctx.progress_disabled) as update:
            if slice_type == "xt":
                result = core.create_xt_slice(
                    media, fixed, start_frame, end_frame,
                    start, end, sample_every, progress=update,
                )
            else:
                result = core.create_yt_slice(
                    media, fixed, start_frame, end_frame,
                    start, end, sample_every, progress=update,
                )

        # result.array 形状 [T, 空间, 3]：纵轴=时间，横轴=空间
        img = scale_nearest(result.array, scale_space, scale_t)
        output_path: str | None = None
        if output is not None:
            save_png(img, output)
            output_path = str(output)

        metadata = SpacetimeMetadata(
            slice_type=slice_type,
            fixed_coordinate=fixed,
            start_frame=result.frame_range.start,
            end_frame=result.frame_range.end,
            frame_count=len(result.frames),
            sample_every=result.frame_range.sample_every,
            space_axis="original_x" if slice_type == "xt" else "original_y",
            time_axis="vertical",
            width=int(img.shape[1]),
            height=int(img.shape[0]),
            scale_space=scale_space,
            scale_t=scale_t,
            output_path=output_path,
        )
        data = metadata.model_dump()
        data["frames"] = result.frames
        data["times"] = result.times
        if ctx.json_mode:
            json_writer.print_success(slice_type, data)
            return
        if ctx.quiet:
            out_console.print(output_path or f"{img.shape[1]}x{img.shape[0]}",
                              highlight=False)
            return
        fixed_name = "y" if slice_type == "xt" else "x"
        print_kv(
            "X–T 切片" if slice_type == "xt" else "Y–T 切片",
            [
                (f"扫描线 {fixed_name}", fixed),
                ("帧范围", f"{result.frame_range.start}～{result.frame_range.end}"
                          f"（每 {result.frame_range.sample_every} 帧采样）"),
                ("帧数", len(result.frames)),
                ("空间轴", metadata.space_axis),
                ("时间轴", "vertical（从上到下时间递增）"),
                ("输出尺寸", f"{metadata.width}x{metadata.height}"),
                ("输出 PNG", output_path),
            ],
        )


def xt(
    media: Path = typer.Argument(..., help="视频路径"),
    y: int = typer.Option(..., "--y", help="水平扫描线的 y 坐标"),
    start_frame: Optional[int] = typer.Option(
        None, "--start-frame", help="起始帧（含）"
    ),
    end_frame: Optional[int] = typer.Option(
        None, "--end-frame", help="结束帧（含）"
    ),
    start: Optional[float] = typer.Option(None, "--start", help="起始时间（秒）"),
    end: Optional[float] = typer.Option(None, "--end", help="结束时间（秒）"),
    sample_every: int = typer.Option(1, "--sample-every", help="每隔 N 帧采样一次"),
    scale_x: int = typer.Option(1, "--scale-x", help="空间轴最近邻放大倍数"),
    scale_t: int = typer.Option(1, "--scale-t", help="时间轴最近邻放大倍数"),
    output: Optional[Path] = typer.Option(None, "--output", help="输出 PNG 路径"),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """生成固定水平扫描线的 X–T 时空切片（横轴=原视频 x，纵轴=时间）。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    _run_slice("xt", media, y, start_frame, end_frame, start, end,
               sample_every, scale_x, scale_t, output, ctx)


def yt(
    media: Path = typer.Argument(..., help="视频路径"),
    x: int = typer.Option(..., "--x", help="垂直扫描线的 x 坐标"),
    start_frame: Optional[int] = typer.Option(
        None, "--start-frame", help="起始帧（含）"
    ),
    end_frame: Optional[int] = typer.Option(
        None, "--end-frame", help="结束帧（含）"
    ),
    start: Optional[float] = typer.Option(None, "--start", help="起始时间（秒）"),
    end: Optional[float] = typer.Option(None, "--end", help="结束时间（秒）"),
    sample_every: int = typer.Option(1, "--sample-every", help="每隔 N 帧采样一次"),
    scale_y: int = typer.Option(1, "--scale-y", help="空间轴最近邻放大倍数"),
    scale_t: int = typer.Option(1, "--scale-t", help="时间轴最近邻放大倍数"),
    output: Optional[Path] = typer.Option(None, "--output", help="输出 PNG 路径"),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """生成固定垂直扫描线的 Y–T 时空切片（横轴=原视频 y，纵轴=时间）。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    _run_slice("yt", media, x, start_frame, end_frame, start, end,
               sample_every, scale_y, scale_t, output, ctx)
