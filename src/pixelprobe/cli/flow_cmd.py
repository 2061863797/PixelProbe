"""pixelprobe flow：稠密光流分析（需可选依赖 pixelprobe[flow]）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

import pixelprobe
from pixelprobe.address import check, pair_options, range_options
from pixelprobe.cli import (
    AT_OPT,
    JSON_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    REF_FILE_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    read_address,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png
from pixelprobe.request_builders import flow_request


def flow(
    media: Path = typer.Argument(..., help="视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    accumulate: bool = typer.Option(
        False, "--accumulate",
        help='累积模式：对 --at 给出的帧范围逐对光流相加，如 --at "f=0..30 step=2"',
    ),
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
    """稠密光流：--at "f=0,4" 比较两帧，或 --accumulate --at "f=0..30"；输出方向、幅度、全局运动与 bbox。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("flow", ctx):
        address = read_address(at, ref_file)
        if accumulate:
            check(address, command="flow --accumulate",
                  time_modes={"range", "all"}, step=True)
            when = range_options(address)
        else:
            check(address, command="flow", time_modes={"list"})
            when = pair_options(address)
        request = flow_request(
            media,
            **when,
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
