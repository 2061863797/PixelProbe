"""pixelprobe compare：比较视频中任意两帧，输出差异热力图与变化区域。"""

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
    with_refs,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv
from pixelprobe.output.image_writer import save_png


def compare(
    media: Path = typer.Argument(..., help="视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    threshold: int = typer.Option(
        10, "--threshold", help="变化像素判定阈值（每像素三通道最大差，0-255）"
    ),
    colormap: str = typer.Option(
        "fire", "--colormap", help="差异图伪彩方案：gray/fire"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="差异热力图 PNG 输出路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """比较两帧：--at "f=0,5"，可加 rect(...)/cell(...)；bbox 为原始分辨率坐标。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("compare", ctx):
        outcome = run_capability(
            "compare", media, at, ref_file, {"threshold": threshold, "colormap": colormap},
        )
        data = dict(outcome.data)
        if output is not None:
            save_png(outcome.images["diff"](), output)
        data["output_path"] = str(output) if output else None
        with_refs(data, outcome)
        if ctx.json_mode:
            json_writer.print_success("compare", data)
            return
        if ctx.quiet:
            out_console.print(f"{data['changed_pixels']} {data['changed_ratio']}", highlight=False)
            return
        bbox = data["bbox"]
        rows: list[tuple[str, object]] = [
            ("帧 A / 帧 B", f"{data['frame_a']} / {data['frame_b']}"),
            ("平均绝对差", data["mean_abs_diff"]),
            ("最大绝对差", data["max_abs_diff"]),
            ("变化像素", f"{data['changed_pixels']}"
             f"（{data['changed_ratio']:.2%}，阈值 {data['threshold']}）"),
            ("变化区域 bbox",
             f"({bbox['x']}, {bbox['y']}, {bbox['width']}, {bbox['height']})"
             if bbox else "无（低于阈值）"),
        ]
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("两帧比较", rows)
