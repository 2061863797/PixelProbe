"""pixelprobe frame：提取视频中的指定帧（可裁剪、可预览缩放）。

注意：--max-width / --max-height 只影响导出的预览图，
像素分析永远基于原始分辨率。
"""

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
from pixelprobe.output.console import out_console, print_kv
from pixelprobe.output.image_writer import fit_within, save_png


def frame(
    media: Path = typer.Argument(..., help="视频（或图片）路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    max_width: Optional[int] = typer.Option(
        None, "--max-width", help="预览图最大宽度（保持宽高比，仅影响导出）"
    ),
    max_height: Optional[int] = typer.Option(
        None, "--max-height", help="预览图最大高度（保持宽高比，仅影响导出）"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="输出 PNG 路径；不指定则只返回帧信息"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """提取指定帧：--at "f=120" 或 "t=3.5s"，可加 rect(...)/cell(...) 裁剪；默认第 0 帧。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("frame", ctx):
        outcome = run_capability("frame", media, at, ref_file)
        data = dict(outcome.data)
        output_width: int | None = None
        output_height: int | None = None
        if output is not None:
            out_arr = fit_within(outcome.images["frame"](), max_width, max_height)
            save_png(out_arr, output)
            output_height, output_width = out_arr.shape[:2]
        data.update(
            output_path=str(output) if output else None,
            output_width=output_width,
            output_height=output_height,
        )
        if ctx.json_mode:
            json_writer.print_success("frame", data)
            return
        if ctx.quiet:
            out_console.print(str(output) if output else f"frame={data['frame']}", highlight=False)
            return
        rows: list[tuple[str, object]] = [
            ("帧号", data["frame"]),
            ("时间(秒)", data["time_seconds"]),
            ("画面尺寸", f"{data['width']}x{data['height']}"),
        ]
        if data["crop"]:
            crop = data["crop"]
            rows.append(("裁剪", f"{crop['x']},{crop['y']},{crop['width']},{crop['height']}"))
        if output:
            rows.append(("输出文件", str(output)))
            rows.append(("输出尺寸", f"{output_width}x{output_height}"))
        print_kv("帧提取", rows)
