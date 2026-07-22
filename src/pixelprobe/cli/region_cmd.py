"""pixelprobe region：分析图片或视频某一帧中的矩形区域。"""

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
from pixelprobe.output.console import out_console, print_kv
from pixelprobe.output.image_writer import save_png
from pixelprobe.utils.coordinates import parse_rect
from pixelprobe.utils.timecode import seconds_to_ms


def region(
    media: Path = typer.Argument(..., help="图片或视频路径"),
    rect: str = typer.Option(
        ..., "--rect", help="矩形区域 x,y,width,height"
    ),
    frame_index: Optional[int] = typer.Option(
        None, "--frame", help="视频帧号（从 0 开始），与 --time 二选一"
    ),
    time: Optional[float] = typer.Option(
        None, "--time", help="视频时间（秒），与 --frame 二选一"
    ),
    output_crop: Optional[Path] = typer.Option(
        None, "--output-crop", help="把区域裁剪图保存为 PNG"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """计算矩形区域的 RGB / HSV / 亮度统计信息。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("region", ctx):
        rect_tuple = parse_rect(rect)
        arr, idx, t, media_info = core.load_frame(
            media, frame=frame_index, time=time
        )
        stats = core.analyze_region(arr, rect_tuple)

        crop_path: str | None = None
        if output_crop is not None:
            x, y, w, h = rect_tuple
            save_png(arr[y : y + h, x : x + w, :], output_crop)
            crop_path = str(output_crop)

        data = {
            "path": media_info.path,
            "media_type": media_info.media_type,
            "frame": idx,
            "time_seconds": t,
            "time_ms": seconds_to_ms(t) if t is not None else None,
            "statistics": stats.model_dump(),
            "crop_path": crop_path,
        }
        if ctx.json_mode:
            json_writer.print_success("region", data)
            return
        if ctx.quiet:
            m = stats.mean_rgb
            out_console.print(
                f"mean_rgb=({m.r},{m.g},{m.b})", highlight=False
            )
            return
        title = "区域统计"
        if idx is not None:
            title += f"（帧 {idx}）"
        print_kv(
            title,
            [
                ("区域", rect),
                ("像素数量", stats.pixel_count),
                ("平均 RGB", f"({stats.mean_rgb.r}, {stats.mean_rgb.g}, {stats.mean_rgb.b})"),
                ("中位数 RGB", f"({stats.median_rgb.r}, {stats.median_rgb.g}, {stats.median_rgb.b})"),
                ("最小 RGB", f"({stats.min_rgb.r}, {stats.min_rgb.g}, {stats.min_rgb.b})"),
                ("最大 RGB", f"({stats.max_rgb.r}, {stats.max_rgb.g}, {stats.max_rgb.b})"),
                ("RGB 标准差", f"({stats.std_rgb.r}, {stats.std_rgb.g}, {stats.std_rgb.b})"),
                ("平均 HSV", f"({stats.mean_hsv.h}, {stats.mean_hsv.s}, {stats.mean_hsv.v})"),
                ("平均亮度", stats.mean_luminance),
                ("亮度标准差", stats.std_luminance),
                ("裁剪图", crop_path),
            ],
        )
