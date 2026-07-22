"""pixelprobe compare：比较视频中任意两帧，输出差异热力图与变化区域。"""

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


def compare(
    media: Path = typer.Argument(..., help="视频路径"),
    frame_a: Optional[int] = typer.Option(
        None, "--frame-a", help="帧 A 帧号，与 --time-a 二选一"
    ),
    time_a: Optional[float] = typer.Option(
        None, "--time-a", help="帧 A 时间（秒），与 --frame-a 二选一"
    ),
    frame_b: Optional[int] = typer.Option(
        None, "--frame-b", help="帧 B 帧号，与 --time-b 二选一"
    ),
    time_b: Optional[float] = typer.Option(
        None, "--time-b", help="帧 B 时间（秒），与 --frame-b 二选一"
    ),
    rect: Optional[str] = typer.Option(
        None, "--rect", help="只比较子区域 x,y,width,height"
    ),
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
    """比较两帧：具体哪里变了、变了多少（bbox 为原始分辨率坐标）。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("compare", ctx):
        rect_tuple = parse_rect(rect) if rect else None
        result = core.compare_frames(
            media,
            frame_a=frame_a,
            time_a=time_a,
            frame_b=frame_b,
            time_b=time_b,
            rect=rect_tuple,
            threshold=threshold,
            colormap=colormap,  # type: ignore[arg-type]
        )
        if output is not None:
            save_png(result.diff_image, output)

        data = {
            "frame_a": result.frame_a,
            "frame_b": result.frame_b,
            "time_a": result.time_a,
            "time_b": result.time_b,
            "rect": (
                {"x": rect_tuple[0], "y": rect_tuple[1],
                 "width": rect_tuple[2], "height": rect_tuple[3]}
                if rect_tuple else None
            ),
            "threshold": result.threshold,
            "mean_abs_diff": result.mean_abs_diff,
            "max_abs_diff": result.max_abs_diff,
            "changed_pixels": result.changed_pixels,
            "changed_ratio": result.changed_ratio,
            "bbox": (
                {"x": result.bbox[0], "y": result.bbox[1],
                 "width": result.bbox[2], "height": result.bbox[3]}
                if result.bbox else None
            ),
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("compare", data)
            return
        if ctx.quiet:
            out_console.print(
                f"{result.changed_pixels} {result.changed_ratio}",
                highlight=False,
            )
            return
        rows: list[tuple[str, object]] = [
            ("帧 A / 帧 B", f"{result.frame_a} / {result.frame_b}"),
            ("平均绝对差", result.mean_abs_diff),
            ("最大绝对差", result.max_abs_diff),
            ("变化像素", f"{result.changed_pixels}"
             f"（{result.changed_ratio:.2%}，阈值 {result.threshold}）"),
            ("变化区域 bbox",
             f"{result.bbox}" if result.bbox else "无（低于阈值）"),
        ]
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("两帧比较", rows)
