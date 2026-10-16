"""pixelprobe spectrum / spectrum2d：时间域主频检测与单帧空间频谱。"""

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
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png


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
        with progress_bar("频谱分析", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "spectrum", media, at, ref_file, {"source": source}, progress=update,
            )
        data = dict(outcome.data)
        if output is not None:
            save_png(outcome.images["spectrum"](), output)
        data["output_path"] = str(output) if output else None
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
        outcome = run_capability("spectrum2d", media, at, ref_file)
        data = dict(outcome.data)
        if output is not None:
            save_png(outcome.images["spectrum"](), output)
        data["output_path"] = str(output) if output else None
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
