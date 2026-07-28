"""pixelprobe spectrum / spectrum2d：时间域主频检测与单帧空间频谱。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

import pixelprobe
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
from pixelprobe.compat.legacy_requests import (
    legacy_spatial_spectrum_request,
    legacy_temporal_spectrum_request,
)


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
        request = legacy_temporal_spectrum_request(
            media,
            source=source,
            rect=rect_tuple,
            point=point_tuple,
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            sample_every=sample_every,
        )
        with progress_bar("频谱分析", 1, ctx.progress_disabled) as update:
            generated = pixelprobe.generate(request)
            update(1, 1)
        data_tensor, preview_tensor = generated.request_tensors[0]
        attributes = data_tensor.attributes
        spectrum_image = preview_tensor.data.materialize()
        if output is not None:
            save_png(spectrum_image, output)

        data = {
            "source": source,
            "samples": attributes["samples"],
            "effective_fps": attributes["effective_fps"],
            "nyquist_hz": attributes["nyquist_hz"],
            "dominant_freq_hz": attributes["dominant_freq_hz"],
            "period_seconds": attributes["period_seconds"],
            "period_frames": attributes["period_frames"],
            "peak_ratio": attributes["peak_ratio"],
            "top_peaks": attributes["top_peaks"],
            "vfr_warning": attributes["vfr_warning"],
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("spectrum", data)
            return
        if ctx.quiet:
            out_console.print(str(data["dominant_freq_hz"]), highlight=False)
            return
        rows: list[tuple[str, object]] = [
            ("序列", f"{source}（{data['samples']} 个采样）"),
            ("主频",
             f"{data['dominant_freq_hz']} Hz" if data["dominant_freq_hz"]
             else "无（序列平坦）"),
            ("周期",
             f"{data['period_seconds']}s / {data['period_frames']} 帧"
             if data["period_seconds"] else "–"),
            ("主峰占比", data["peak_ratio"]),
            ("可检上限",
             f"{data['nyquist_hz']} Hz（更高频率会混叠或漏采）"),
        ]
        if data["vfr_warning"]:
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
        request = legacy_spatial_spectrum_request(
            media, frame=frame, time=time, rect=rect_tuple,
        )
        generated = pixelprobe.generate(request)
        data_tensor, preview_tensor = generated.request_tensors[0]
        attributes = data_tensor.attributes
        spectrum_image = preview_tensor.data.materialize()
        if output is not None:
            save_png(spectrum_image, output)

        image_semantics = bool(request.feature.config["report_image_semantics"])

        data = {
            "frame": None if image_semantics else attributes["frame"],
            "time_seconds": None if image_semantics else attributes["time_seconds"],
            "width": attributes["width"],
            "height": attributes["height"],
            "peaks": attributes["peaks"],
            "output_path": str(output) if output else None,
        }
        if ctx.json_mode:
            json_writer.print_success("spectrum2d", data)
            return
        if ctx.quiet:
            out_console.print(str(len(data["peaks"])), highlight=False)
            return
        rows: list[tuple[str, object]] = [
            ("分析区域", f"{data['width']}x{data['height']}"),
            ("显著峰数", len(data["peaks"])),
        ]
        for i, p in enumerate(data["peaks"][:3]):
            rows.append((
                f"峰 {i + 1}",
                f"周期 {p['period_px']}px 方向 {p['angle_deg']}°",
            ))
        if output:
            rows.append(("输出文件", str(output)))
        print_kv("空间频谱", rows)
