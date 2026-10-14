"""pixelprobe sheet：等距抽帧拼接采样网格图（contact sheet）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe import core
from pixelprobe.address import check, range_options
from pixelprobe.cli import (
    AT_OPT,
    JSON_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    REF_FILE_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    read_address,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png


def sheet(
    media: Path = typer.Argument(..., help="视频路径"),
    count: int = typer.Option(9, "--count", help="抽取帧数"),
    cols: Optional[int] = typer.Option(
        None, "--cols", help="网格列数（缺省自动取近似方形）"
    ),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    tile_max_dim: int = typer.Option(
        320, "--tile-max-dim", help="单格最大边长（像素）"
    ),
    no_annotate: bool = typer.Option(
        False, "--no-annotate", help="不绘制帧号/时间标注条"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="网格图 PNG 输出路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """一张图概览视频：在 --at 给出的时间段（默认全片）内等距抽 N 帧拼网格，标注帧号和秒。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("sheet", ctx):
        address = read_address(at, ref_file)
        check(address, command="sheet", time_modes={"range", "all"})
        when = range_options(address)
        when.pop("sample_every")
        with progress_bar("抽帧拼图", 1, ctx.progress_disabled) as update:
            result = core.sample_frames(
                media,
                count=count,
                cols=cols,
                **when,
                tile_max_dim=tile_max_dim,
                annotate=not no_annotate,
                progress=update,
            )
        if output is not None:
            save_png(result.image, output)

        data = {
            "frames": result.frames,
            "times": result.times,
            "cols": result.cols,
            "rows": result.rows,
            "tile_width": result.tile_width,
            "tile_height": result.tile_height,
            "sheet_width": int(result.image.shape[1]),
            "sheet_height": int(result.image.shape[0]),
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("sheet", data)
            return
        if ctx.quiet:
            out_console.print(
                str(output) if output else " ".join(map(str, result.frames)),
                highlight=False,
            )
            return
        rows: list[tuple[str, object]] = [
            ("抽取帧", ", ".join(map(str, result.frames))),
            ("网格", f"{result.cols} 列 × {result.rows} 行"),
            ("整图尺寸",
             f"{data['sheet_width']}x{data['sheet_height']}"),
        ]
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("采样网格", rows)
