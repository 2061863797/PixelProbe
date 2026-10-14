"""pixelprobe spectrum / spectrum2d：时间域主频检测与单帧空间频谱。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

import pixelprobe
from pixelprobe.address import (
    check,
    points_of,
    range_options,
    rect_of,
    single_frame_options,
)
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
from pixelprobe.models.errors import AddressError
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png
from pixelprobe.request_builders import (
    spatial_spectrum_request,
    temporal_spectrum_request,
)


def spectrum(
    media: Path = typer.Argument(..., help="视频路径"),
    source: str = typer.Option(
        "luma", "--source", help="分析序列：luma（亮度）/ change（相邻帧变化量）"
    ),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    output: Optional[Path] = typer.Option(
        None, "--output", help="谱线图 PNG 输出路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """周期闪烁/周期变化的主频：--at "t=0s..10s rect(...)" 或 "point(x,y)"；默认整帧全片。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("spectrum", ctx):
        address = read_address(at, ref_file)
        check(
            address, command="spectrum", time_modes={"range", "all"},
            shapes={"point", "pid", "rect", "cell"}, step=True,
        )
        width, height = media_size(media, address)
        points = points_of(address, width, height)
        if len(points) > 1:
            raise AddressError("spectrum 一次只能分析一个点")
        request = temporal_spectrum_request(
            media,
            source=source,
            rect=rect_of(address, width, height),
            point=points[0] if points else None,
            **range_options(address),
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
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    output: Optional[Path] = typer.Option(
        None, "--output", help="频谱图 PNG 输出路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """条纹/摩尔纹/周期纹理：单帧灰度二维 FFT，--at "f=15 rect(0,0,256,256)"；默认第 0 帧整幅。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("spectrum2d", ctx):
        address = read_address(at, ref_file)
        check(address, command="spectrum2d", time_modes={"single"}, shapes={"rect", "cell"})
        request = spatial_spectrum_request(
            media, **single_frame_options(address),
            rect=rect_of(address, *media_size(media, address)),
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
