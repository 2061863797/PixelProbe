"""pixelprobe frame：提取视频中的指定帧（可裁剪、可预览缩放）。

注意：--max-width / --max-height 只影响导出的预览图，
像素分析永远基于原始分辨率。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe import core
from pixelprobe.address import check, rect_of, single_frame_options
from pixelprobe.cli import (
    AT_OPT,
    JSON_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    REF_FILE_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    media_size,
    read_address,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv
from pixelprobe.output.image_writer import fit_within, save_png
from pixelprobe.utils.timecode import seconds_to_ms


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
        address = read_address(at, ref_file)
        check(address, command="frame", time_modes={"single"}, shapes={"rect", "cell"})
        crop_rect = rect_of(address, *media_size(media, address))
        arr, idx, t, media_info = core.get_frame(
            media, **single_frame_options(address), crop=crop_rect
        )

        output_width: int | None = None
        output_height: int | None = None
        if output is not None:
            out_arr = fit_within(arr, max_width, max_height)
            save_png(out_arr, output)
            output_height, output_width = out_arr.shape[:2]

        data = {
            "path": media_info.path,
            "media_type": media_info.media_type,
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "source_width": media_info.width,
            "source_height": media_info.height,
            "width": int(arr.shape[1]),
            "height": int(arr.shape[0]),
            "crop": (
                {
                    "x": crop_rect[0],
                    "y": crop_rect[1],
                    "width": crop_rect[2],
                    "height": crop_rect[3],
                }
                if crop_rect
                else None
            ),
            "output_path": str(output) if output else None,
            "output_width": output_width,
            "output_height": output_height,
        }
        if ctx.json_mode:
            json_writer.print_success("frame", data)
            return
        if ctx.quiet:
            out_console.print(str(output) if output else f"frame={idx}", highlight=False)
            return
        rows: list[tuple[str, object]] = [
            ("帧号", idx),
            ("时间(秒)", t),
            ("画面尺寸", f"{data['width']}x{data['height']}"),
        ]
        if crop_rect:
            rows.append(("裁剪", ",".join(map(str, crop_rect))))
        if output:
            rows.append(("输出文件", str(output)))
            rows.append(("输出尺寸", f"{output_width}x{output_height}"))
        print_kv("帧提取", rows)
