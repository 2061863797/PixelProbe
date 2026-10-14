"""pixelprobe timeline：提取固定像素在帧序列中的颜色变化。

输出图片默认方向：横轴为时间（T 帧），纵轴为像素点（K 个）。
--scale 使用最近邻放大；scale > 1 时同时保存未放大的 .raw.png。
"""

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
from pixelprobe.models.timeline import TimelineMetadata
from pixelprobe.output import csv_writer, json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png, scale_nearest
from pixelprobe.utils.validation import ensure_scale


def timeline(
    media: Path = typer.Argument(..., help="视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    block_size: Optional[int] = typer.Option(
        None, "--block-size",
        help="像素块边长 N：每个采样位置取 N×N 块的平均 RGB（与 grid(...) 搭配）",
    ),
    sort: str = typer.Option(
        "selection", "--sort",
        help="像素行排序：selection | pixel-id | yx | xy",
    ),
    orientation: str = typer.Option(
        "horizontal", "--orientation",
        help="图像方向：horizontal（横轴时间）| vertical（纵轴时间）",
    ),
    scale: int = typer.Option(
        1, "--scale", help="最近邻放大倍数（每个颜色格变为 N×N 方块）"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", help="输出 PNG 路径"
    ),
    csv: Optional[Path] = typer.Option(
        None, "--csv", help="输出 CSV 路径"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """固定像素的颜色时间线 [K,T,3]：--at "t=1s..3s point(24,16) point(2,28)" 或 "grid(0,0,32,32 step=4)"。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("timeline", ctx):
        if sort not in ("selection", "pixel-id", "yx", "xy"):
            raise typer.BadParameter(
                f"--sort {sort} 无效，可选 selection|pixel-id|yx|xy"
            )
        if orientation not in ("horizontal", "vertical"):
            raise typer.BadParameter(
                f"--orientation {orientation} 无效，可选 horizontal|vertical"
            )
        ensure_scale(scale, "--scale")
        with progress_bar("提取时间线", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "timeline", media, at, ref_file,
                {"block_size": block_size, "sort": sort}, progress=update,
            )
        result = outcome.artifacts["timeline"]

        # 矩阵 [K,T,3] 本身就是 horizontal 图像（行=像素点，列=时间）
        raw_img = (
            result.matrix
            if orientation == "horizontal"
            else result.matrix.transpose(1, 0, 2)
        )
        raw_h, raw_w = raw_img.shape[:2]

        output_path: str | None = None
        raw_output_path: str | None = None
        if output is not None:
            preview = scale_nearest(raw_img, scale, scale)
            save_png(preview, output)
            output_path = str(output)
            if scale > 1:
                raw_path = output.with_name(f"{output.stem}.raw{output.suffix}")
                save_png(raw_img, raw_path)
                raw_output_path = str(raw_path)
        csv_path: str | None = None
        if csv is not None:
            csv_writer.write_timeline_csv(csv, result)
            csv_path = str(csv)

        metadata = TimelineMetadata(
            points=result.points,
            start_frame=result.frame_range.start,
            end_frame=result.frame_range.end,
            frame_count=len(result.frames),
            sample_every=result.frame_range.sample_every,
            orientation=orientation,  # type: ignore[arg-type]
            sort=result.sort,
            sample_type=result.sample_type,
            block_size=result.block_size,
            raw_width=raw_w,
            raw_height=raw_h,
            scale=scale,
            output_path=output_path,
            raw_output_path=raw_output_path,
            csv_path=csv_path,
        )
        data = metadata.model_dump()
        data["frames"] = result.frames
        data["times"] = result.times
        if ctx.json_mode:
            json_writer.print_success("timeline", data)
            return
        if ctx.quiet:
            out_console.print(output_path or f"{raw_w}x{raw_h}", highlight=False)
            return
        print_kv(
            "时间线",
            [
                ("像素点数 K", len(result.points)),
                ("帧数 T", len(result.frames)),
                ("帧范围", f"{result.frame_range.start}～{result.frame_range.end}"
                          f"（每 {result.frame_range.sample_every} 帧采样）"),
                ("采样方式", result.sample_type
                            + (f"（block={result.block_size}）"
                               if result.block_size else "")),
                ("原始矩阵", f"{raw_w}x{raw_h}（宽=时间，高=像素点）"
                 if orientation == "horizontal"
                 else f"{raw_w}x{raw_h}（宽=像素点，高=时间）"),
                ("输出 PNG", output_path),
                ("原始 PNG", raw_output_path),
                ("输出 CSV", csv_path),
            ],
        )
