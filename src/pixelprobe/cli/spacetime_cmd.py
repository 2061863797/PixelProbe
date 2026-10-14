"""pixelprobe xt / yt：时空切片命令。

- xt：固定 y 的水平扫描线，横轴=原视频 x，纵轴=时间；
- yt：固定 x 的垂直扫描线，横轴=原视频 y，纵轴=时间。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

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
from pixelprobe.models.spacetime import SpacetimeMetadata
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png, scale_nearest
from pixelprobe.utils.validation import ensure_scale


def _run_slice(
    slice_type: Literal["xt", "yt"],
    media: Path,
    at: Optional[str],
    ref_file: Optional[Path],
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
            outcome = run_capability(slice_type, media, at, ref_file, progress=update)
        core_data = outcome.data
        # 规范 Tensor 形状 [T, 空间, 3]：纵轴=时间，横轴=空间。
        img = scale_nearest(outcome.images["slice"](), scale_space, scale_t)
        output_path: str | None = None
        if output is not None:
            save_png(img, output)
            output_path = str(output)
        metadata = SpacetimeMetadata(
            slice_type=slice_type,
            fixed_coordinate=core_data["fixed_coordinate"],
            start_frame=core_data["start_frame"],
            end_frame=core_data["end_frame"],
            frame_count=core_data["frame_count"],
            sample_every=core_data["sample_every"],
            space_axis=core_data["space_axis"],
            time_axis="vertical",
            width=int(img.shape[1]),
            height=int(img.shape[0]),
            scale_space=scale_space,
            scale_t=scale_t,
            output_path=output_path,
        )
        data = metadata.model_dump()
        data["frames"] = core_data["frames"]
        data["times"] = core_data["times"]
        if ctx.json_mode:
            json_writer.print_success(slice_type, data)
            return
        if ctx.quiet:
            out_console.print(output_path or f"{img.shape[1]}x{img.shape[0]}", highlight=False)
            return
        fixed_name = "y" if slice_type == "xt" else "x"
        print_kv(
            "X–T 切片" if slice_type == "xt" else "Y–T 切片",
            [
                (f"扫描线 {fixed_name}", metadata.fixed_coordinate),
                ("帧范围", f"{metadata.start_frame}～{metadata.end_frame}"
                          f"（每 {metadata.sample_every} 帧采样）"),
                ("帧数", metadata.frame_count),
                ("空间轴", metadata.space_axis),
                ("时间轴", "vertical（从上到下时间递增）"),
                ("输出尺寸", f"{metadata.width}x{metadata.height}"),
                ("输出 PNG", output_path),
            ],
        )


def xt(
    media: Path = typer.Argument(..., help="视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    scale_x: int = typer.Option(1, "--scale-x", help="空间轴最近邻放大倍数"),
    scale_t: int = typer.Option(1, "--scale-t", help="时间轴最近邻放大倍数"),
    output: Optional[Path] = typer.Option(None, "--output", help="输出 PNG 路径"),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """X–T 时空切片（横轴=原视频 x，纵轴=时间）：--at "row(120)"，可加时间范围与 step。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    _run_slice("xt", media, at, ref_file, scale_x, scale_t, output, ctx)


def yt(
    media: Path = typer.Argument(..., help="视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    scale_y: int = typer.Option(1, "--scale-y", help="空间轴最近邻放大倍数"),
    scale_t: int = typer.Option(1, "--scale-t", help="时间轴最近邻放大倍数"),
    output: Optional[Path] = typer.Option(None, "--output", help="输出 PNG 路径"),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """Y–T 时空切片（横轴=原视频 y，纵轴=时间）：--at "col(40)"，可加时间范围与 step。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    _run_slice("yt", media, at, ref_file, scale_y, scale_t, output, ctx)
