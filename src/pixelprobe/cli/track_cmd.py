"""pixelprobe track：点跟踪与跟随切片。"""

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
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png


def track(
    media: Path = typer.Argument(..., help="视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    patch: int = typer.Option(8, "--patch", help="跟踪块半径：模板为 (2p+1)×(2p+1) 像素"),
    search: int = typer.Option(16, "--search", help="每帧搜索半径（像素）"),
    lost_threshold: float = typer.Option(
        40.0, "--lost-threshold", help="最小平均绝对差超过此值判定跟丢并停止（亮度 0～255）",
    ),
    strip_width: int = typer.Option(64, "--strip-width", help="跟随切片每行宽度（像素）"),
    follow_output: Optional[Path] = typer.Option(None, "--follow-output", help="跟随切片 PNG 输出路径"),
    path_output: Optional[Path] = typer.Option(None, "--path-output", help="首帧轨迹图 PNG 输出路径"),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """从一个点开始逐帧跟踪：--at "f=10..80 point(120,200)"。

    输出每帧坐标、位移与速度；跟随切片让被跟踪物体始终处于中线，便于观察它自身的变化。
    整数像素精度，匹配误差超过阈值即判定跟丢并停止。
    """
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("track", ctx):
        with progress_bar("点跟踪", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "track", media, at, ref_file,
                {"patch": patch, "search": search, "lost_threshold": lost_threshold,
                 "strip_width": strip_width},
                progress=update,
            )
        data = with_refs(dict(outcome.data), outcome)
        if follow_output is not None:
            save_png(outcome.images["follow"](), follow_output)
        if path_output is not None:
            save_png(outcome.images["path"](), path_output)
        data.update(
            follow_output_path=str(follow_output) if follow_output else None,
            path_output_path=str(path_output) if path_output else None,
        )
        if ctx.json_mode:
            json_writer.print_success("track", data)
            return
        end = data["end_point"]
        if ctx.quiet:
            out_console.print(f"{data['end_frame']} {end['x']},{end['y']}", highlight=False)
            return
        print_kv("点跟踪", [
            ("帧范围", f"{data['start_frame']}～{data['end_frame']}（{data['tracked_frames']} 帧）"),
            ("起点 → 终点", f"({data['start_point']['x']},{data['start_point']['y']}) → ({end['x']},{end['y']})"),
            ("位移", f"dx={data['displacement']['dx']} dy={data['displacement']['dy']}"),
            ("路径长度", f"{data['path_length_pixels']} 像素"),
            ("平均速度", f"{data['mean_speed_pixels_per_second']} 像素/秒"),
            ("跟丢", "否" if data["lost_at"] is None else f"第 {data['lost_at']} 帧"),
            ("跟随切片", data["follow_output_path"]),
            ("轨迹图", data["path_output_path"]),
        ])
