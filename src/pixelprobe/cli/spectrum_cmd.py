"""pixelprobe spectrum / spectrum2d：时间域主频检测与单帧空间频谱。"""

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
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png
from pixelprobe.utils.coordinates import parse_point, parse_rect


def spectrum(
    media: Path = typer.Argument(..., help="视频路径"),
    source: str = typer.Option(
        "luma", "--source", help="分析序列：luma（亮度）/ change（相邻帧变化量）"
    ),
    rect: Optional[str] = typer.Option(
        None, "--rect", help="只统计子区域 x,y,width,height"
    ),
    point: Optional[str] = typer.Option(
        None, "--point", help="只统计单像素 x,y"
    ),
    start_frame: Optional[int] = typer.Option(
        None, "--start-frame", help="起始帧（含）"
    ),
    end_frame: Optional[int] = typer.Option(
        None, "--end-frame", help="结束帧（含）"
    ),
    start: Optional[float] = typer.Option(None, "--start", help="起始时间（秒）"),
    end: Optional[float] = typer.Option(None, "--end", help="结束时间（秒）"),
    sample_every: int = typer.Option(1, "--sample-every", help="每隔 N 帧采样一次"),
    output: Optional[Path] = typer.Option(
        None, "--output", help="谱线图 PNG 输出路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """检测周期闪烁/周期变化：对亮度或变化序列做 FFT，报告主频。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("spectrum", ctx):
        rect_tuple = parse_rect(rect) if rect else None
        point_tuple = parse_point(point) if point else None
        with progress_bar("频谱分析", 1, ctx.progress_disabled) as update:
            result = core.temporal_spectrum(
                media,
                source=source,  # type: ignore[arg-type]
                rect=rect_tuple,
                point=point_tuple,
                start_frame=start_frame,
                end_frame=end_frame,
                start=start,
                end=end,
                sample_every=sample_every,
                progress=update,
            )
        if output is not None:
            save_png(result.spectrum_image, output)

        data = {
            "source": result.source,
            "samples": result.samples,
            "effective_fps": result.effective_fps,
            "dominant_freq_hz": result.dominant_freq_hz,
            "period_seconds": result.period_seconds,
            "period_frames": result.period_frames,
            "peak_ratio": result.peak_ratio,
            "top_peaks": result.top_peaks,
            "vfr_warning": result.vfr_warning,
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("spectrum", data)
            return
        if ctx.quiet:
            out_console.print(str(result.dominant_freq_hz), highlight=False)
            return
        rows: list[tuple[str, object]] = [
            ("序列", f"{result.source}（{result.samples} 个采样）"),
            ("主频",
             f"{result.dominant_freq_hz} Hz" if result.dominant_freq_hz
             else "无（序列平坦）"),
            ("周期",
             f"{result.period_seconds}s / {result.period_frames} 帧"
             if result.period_seconds else "–"),
            ("主峰占比", result.peak_ratio),
        ]
        if result.vfr_warning:
            rows.append(("警告", "可变帧率视频，频率按平均帧率换算"))
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("时间域频谱", rows)


def spectrum2d(
    media: Path = typer.Argument(..., help="图片或视频路径"),
    frame: Optional[int] = typer.Option(
        None, "--frame", help="帧号（从 0 开始），与 --time 二选一"
    ),
    time: Optional[float] = typer.Option(
        None, "--time", help="时间（秒），与 --frame 二选一"
    ),
    rect: Optional[str] = typer.Option(
        None, "--rect", help="只分析子区域 x,y,width,height"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="频谱图 PNG 输出路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """检测条纹/摩尔纹/周期纹理：对单帧灰度做二维 FFT。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("spectrum2d", ctx):
        rect_tuple = parse_rect(rect) if rect else None
        result = core.spatial_spectrum(
            media, frame=frame, time=time, rect=rect_tuple
        )
        if output is not None:
            save_png(result.spectrum_image, output)

        data = {
            "frame": result.frame,
            "time_seconds": result.time_seconds,
            "width": result.width,
            "height": result.height,
            "peaks": result.peaks,
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("spectrum2d", data)
            return
        if ctx.quiet:
            out_console.print(str(len(result.peaks)), highlight=False)
            return
        rows: list[tuple[str, object]] = [
            ("分析区域", f"{result.width}x{result.height}"),
            ("显著峰数", len(result.peaks)),
        ]
        for i, p in enumerate(result.peaks[:3]):
            rows.append((
                f"峰 {i + 1}",
                f"周期 {p['period_px']}px 方向 {p['angle_deg']}°",
            ))
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("空间频谱", rows)
