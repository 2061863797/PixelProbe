"""pixelprobe region：分析图片或视频某一帧中的矩形区域。"""

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
from pixelprobe.output.image_writer import save_png


def region(
    media: Path = typer.Argument(..., help="图片或视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    output_crop: Optional[Path] = typer.Option(
        None, "--output-crop", help="把区域裁剪图保存为 PNG"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """计算区域的 RGB / HSV / 亮度统计：--at "f=15 rect(4,4,10,10)" 或 "cell(C4)"。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("region", ctx):
        outcome = run_capability("region", media, at, ref_file)
        data = dict(outcome.data)
        crop_path: str | None = None
        if output_crop is not None:
            save_png(outcome.images["crop"](), output_crop)
            crop_path = str(output_crop)
        data["crop_path"] = crop_path
        if ctx.json_mode:
            json_writer.print_success("region", data)
            return
        stats = data["statistics"]
        if ctx.quiet:
            m = stats["mean_rgb"]
            out_console.print(f"mean_rgb=({m['r']},{m['g']},{m['b']})", highlight=False)
            return
        title = "区域统计"
        if data["frame"] is not None:
            title += f"（帧 {data['frame']}）"

        def triple(key: str) -> str:
            value = stats[key]
            return f"({value['r']}, {value['g']}, {value['b']})"

        hsv = stats["mean_hsv"]
        print_kv(
            title,
            [
                ("区域", ",".join(map(str, outcome.artifacts["rect"]))),
                ("像素数量", stats["pixel_count"]),
                ("平均 RGB", triple("mean_rgb")),
                ("中位数 RGB", triple("median_rgb")),
                ("最小 RGB", triple("min_rgb")),
                ("最大 RGB", triple("max_rgb")),
                ("RGB 标准差", triple("std_rgb")),
                ("平均 HSV", f"({hsv['h']}, {hsv['s']}, {hsv['v']})"),
                ("平均亮度", stats["mean_luminance"]),
                ("亮度标准差", stats["std_luminance"]),
                ("裁剪图", crop_path),
            ],
        )
