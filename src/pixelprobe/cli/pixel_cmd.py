"""pixelprobe pixel：查询图片或视频某一帧中的一个或多个像素。"""

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
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_table


def pixel(
    media: Path = typer.Argument(..., help="图片或视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    sample: str = typer.Option(
        "display", "--sample",
        help="样本类型：display（历史 RGB8）或 native（图片原生样本）",
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """查询像素：--at "f=12 point(520,340) pid(1000)"；``--sample native`` 读取图片原生通道。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("pixel", ctx):
        sample = sample.lower()
        if sample not in {"display", "native"}:
            raise InvalidRangeError("--sample 仅支持 display 或 native")
        data = run_capability("pixel", media, at, ref_file, {"sample": sample}).data
        if ctx.json_mode:
            json_writer.print_success("pixel", data)
            return
        if sample == "native":
            if ctx.quiet:
                for item in data["pixels"]:
                    out_console.print(f"({item['x']},{item['y']}) {item['values']}", highlight=False)
                return
            print_table(
                "原生图片像素查询",
                ["pixel_id", "x", "y", "通道", "值", "dtype", "语义"],
                [
                    [
                        item["pixel_id"], item["x"], item["y"],
                        ",".join(item["channels"]), item["values"],
                        item["dtype"], item["sample_semantics"],
                    ]
                    for item in data["pixels"]
                ],
            )
            return
        if ctx.quiet:
            for item in data["pixels"]:
                out_console.print(f"({item['x']},{item['y']}) {item['hex']}", highlight=False)
            return
        title = "像素查询"
        if data["frame"] is not None:
            title += f"（帧 {data['frame']}）"
        print_table(
            title,
            ["pixel_id", "x", "y", "R", "G", "B", "HEX", "H", "S", "V", "亮度"],
            [
                [item["pixel_id"], item["x"], item["y"],
                 item["rgb"]["r"], item["rgb"]["g"], item["rgb"]["b"], item["hex"],
                 item["hsv"]["h"], item["hsv"]["s"], item["hsv"]["v"], item["luminance"]]
                for item in data["pixels"]
            ],
        )
