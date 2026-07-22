"""pixelprobe pixel：查询图片或视频某一帧中的一个或多个像素。"""

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
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_table
from pixelprobe.utils.coordinates import parse_point, xy_from_pixel_id
from pixelprobe.utils.timecode import seconds_to_ms


def pixel(
    media: Path = typer.Argument(..., help="图片或视频路径"),
    point: Optional[list[str]] = typer.Option(
        None, "--point", help="像素坐标 x,y（可重复指定多个）"
    ),
    pixel_id: Optional[list[int]] = typer.Option(
        None, "--pixel-id", help="像素编号 y*width+x（可重复指定多个）"
    ),
    frame_index: Optional[int] = typer.Option(
        None, "--frame", help="视频帧号（从 0 开始），与 --time 二选一"
    ),
    time: Optional[float] = typer.Option(
        None, "--time", help="视频时间（秒），与 --frame 二选一"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """查询一个或多个像素的 RGB / HSV / 亮度。视频需用 --frame 或 --time 指定帧。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("pixel", ctx):
        arr, idx, t, media_info = core.load_frame(
            media, frame=frame_index, time=time
        )
        points = [parse_point(p) for p in (point or [])]
        points += [
            xy_from_pixel_id(pid, media_info.width, media_info.height)
            for pid in (pixel_id or [])
        ]
        if not points:
            raise InvalidRangeError(
                "请至少指定一个 --point 或 --pixel-id"
            )
        samples = core.inspect_pixels(arr, points, frame=idx, time_seconds=t)

        data = {
            "path": media_info.path,
            "media_type": media_info.media_type,
            "width": media_info.width,
            "height": media_info.height,
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "pixels": [s.model_dump() for s in samples],
        }
        if ctx.json_mode:
            json_writer.print_success("pixel", data)
            return
        if ctx.quiet:
            for s in samples:
                out_console.print(
                    f"({s.x},{s.y}) {s.hex}", highlight=False
                )
            return
        title = "像素查询"
        if idx is not None:
            title += f"（帧 {idx}）"
        print_table(
            title,
            ["pixel_id", "x", "y", "R", "G", "B", "HEX", "H", "S", "V", "亮度"],
            [
                [s.pixel_id, s.x, s.y, s.rgb.r, s.rgb.g, s.rgb.b, s.hex,
                 s.hsv.h, s.hsv.s, s.hsv.v, s.luminance]
                for s in samples
            ],
        )
