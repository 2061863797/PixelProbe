"""pixelprobe flow：稠密光流分析（需可选依赖 pixelprobe[flow]）。"""

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
from pixelprobe.compat.legacy_requests import legacy_flow_request


def flow(
    media: Path = typer.Argument(..., help="视频路径"),
    frame_a: Optional[int] = typer.Option(
        None, "--frame-a", help="帧 A 帧号，与 --time-a 二选一"
    ),
    time_a: Optional[float] = typer.Option(
        None, "--time-a", help="帧 A 时间（秒）"
    ),
    frame_b: Optional[int] = typer.Option(
        None, "--frame-b", help="帧 B 帧号，与 --time-b 二选一"
    ),
    time_b: Optional[float] = typer.Option(
        None, "--time-b", help="帧 B 时间（秒）"
    ),
    accumulate: bool = typer.Option(
        False, "--accumulate", help="累积模式：对帧范围逐对光流相加"
    ),
    start_frame: Optional[int] = typer.Option(
        None, "--start-frame", help="起始帧（含，累积模式）"
    ),
    end_frame: Optional[int] = typer.Option(
        None, "--end-frame", help="结束帧（含，累积模式）"
    ),
    start: Optional[float] = typer.Option(None, "--start", help="起始时间（秒）"),
    end: Optional[float] = typer.Option(None, "--end", help="结束时间（秒）"),
    sample_every: int = typer.Option(1, "--sample-every", help="每隔 N 帧采样一次"),
    compensate: bool = typer.Option(
        False, "--compensate", help="估计并扣除全局运动（区分镜头运动与物体运动）"
    ),
    mag_threshold: float = typer.Option(
        1.0, "--mag-threshold", help="运动区域判定的位移阈值（像素）"
    ),
    flow_output: Optional[Path] = typer.Option(
        None, "--flow-output", help="方向着色流场图 PNG 输出路径"
    ),
    magnitude_output: Optional[Path] = typer.Option(
        None, "--magnitude-output", help="幅度伪彩图 PNG 输出路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """稠密光流：运动方向/幅度、全局运动估计与运动区域 bbox。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("flow", ctx):
        request = legacy_flow_request(
            media,
            frame_a=frame_a,
            time_a=time_a,
            frame_b=frame_b,
            time_b=time_b,
            start_frame=start_frame,
            end_frame=end_frame,
            start=start,
            end=end,
            sample_every=sample_every,
            accumulate=accumulate,
            compensate_global=compensate,
            mag_threshold=mag_threshold,
        )
        with progress_bar("光流分析", 1, ctx.progress_disabled) as update:
            generated = pixelprobe.generate(request)
            update(1, 1)
        tensors = generated.request_tensors[0]
        flow_preview, magnitude_preview = tensors[-2:]
        magnitude_tensor = tensors[-3]
        metadata = magnitude_tensor.attributes
        flow_image = flow_preview.data.materialize()
        magnitude_image = magnitude_preview.data.materialize()
        if flow_output is not None:
            save_png(flow_image, flow_output)
        if magnitude_output is not None:
            save_png(magnitude_image, magnitude_output)

        data = {
            "frame_a": metadata["frame_a"],
            "frame_b": metadata["frame_b"],
            "accumulated": metadata["accumulated"],
            "frames_analyzed": metadata["frames_analyzed"],
            "mean_magnitude": metadata["mean_magnitude"],
            "max_magnitude": metadata["max_magnitude"],
            "p95_magnitude": metadata["p95_magnitude"],
            "dominant_angle_deg": metadata["dominant_angle_deg"],
            "global_motion": metadata["global_motion"],
            "compensated": metadata["compensated"],
            "motion_bbox": (
                {"x": metadata["motion_bbox"][0], "y": metadata["motion_bbox"][1],
                 "width": metadata["motion_bbox"][2], "height": metadata["motion_bbox"][3]}
                if metadata["motion_bbox"] else None
            ),
            "mag_threshold": mag_threshold,
            "flow_output_path": str(flow_output) if flow_output else None,
            "magnitude_output_path": (
                str(magnitude_output) if magnitude_output else None
            ),
        }
        if ctx.json_mode:
            json_writer.print_success("flow", data)
            return
        if ctx.quiet:
            out_console.print(
                f"{data['mean_magnitude']} {data['dominant_angle_deg']}",
                highlight=False,
            )
            return
        gm = data["global_motion"]
        rows: list[tuple[str, object]] = [
            ("帧 A / 帧 B", f"{data['frame_a']} / {data['frame_b']}"
             f"{'（累积）' if data['accumulated'] else ''}"),
            ("平均/最大幅度",
             f"{data['mean_magnitude']} / {data['max_magnitude']} px"),
            ("主方向",
             f"{data['dominant_angle_deg']}°（0°=向右，y 向下）"
             if data["dominant_angle_deg"] is not None else "无明显运动"),
            ("全局运动",
             f"dx={gm['dx']} dy={gm['dy']} 旋转 {gm['rotation_deg']}° "
             f"缩放 {gm['scale']}" if gm else "估计失败"),
            ("运动区域 bbox",
             f"{metadata['motion_bbox']}" if metadata["motion_bbox"] else "无"),
        ]
        if flow_output:
            rows.append(("流场图", str(flow_output)))
        if magnitude_output:
            rows.append(("幅度图", str(magnitude_output)))
        print_kv("光流分析", rows)
