"""pixelprobe timeline：提取固定像素在帧序列中的颜色变化。

输出图片默认方向：横轴为时间（T 帧），纵轴为像素点（K 个）。
--scale 使用最近邻放大；scale > 1 时同时保存未放大的 .raw.png。
"""

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
from pixelprobe.models.timeline import TimelineMetadata
from pixelprobe.output import csv_writer, json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar
from pixelprobe.output.image_writer import save_png, scale_nearest
from pixelprobe.utils.coordinates import parse_point, parse_rect
from pixelprobe.utils.validation import ensure_scale


def timeline(
    media: Path = typer.Argument(..., help="视频路径"),
    point: Optional[list[str]] = typer.Option(
        None, "--point", help="像素坐标 x,y（可重复指定多个）"
    ),
    pixel_id: Optional[list[int]] = typer.Option(
        None, "--pixel-id", help="像素编号（可重复指定多个）"
    ),
    grid: Optional[str] = typer.Option(
        None, "--grid", help="网格采样区域 x,y,width,height"
    ),
    step: Optional[int] = typer.Option(
        None, "--step", help="网格采样步长（与 --grid 搭配）"
    ),
    block_size: Optional[int] = typer.Option(
        None, "--block-size",
        help="像素块边长 N：每个采样位置取 N×N 块的平均 RGB（与 --grid 搭配）",
    ),
    start_frame: Optional[int] = typer.Option(
        None, "--start-frame", help="起始帧（含），与 --start/--end 不能混用"
    ),
    end_frame: Optional[int] = typer.Option(
        None, "--end-frame", help="结束帧（含），与 --start/--end 不能混用"
    ),
    start: Optional[float] = typer.Option(
        None, "--start", help="起始时间（秒），与帧范围不能混用"
    ),
    end: Optional[float] = typer.Option(
        None, "--end", help="结束时间（秒），与帧范围不能混用"
    ),
    sample_every: int = typer.Option(
        1, "--sample-every", help="每隔 N 帧采样一次"
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
    """提取一个或多个固定像素在视频中的颜色时间线，生成 [K,T,3] 矩阵。"""
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
        points = [parse_point(p) for p in (point or [])]
        grid_rect = parse_rect(grid) if grid else None

        with progress_bar("提取时间线", 1, ctx.progress_disabled) as update:
            result = core.extract_timelines(
                media,
                points=points or None,
                pixel_ids=pixel_id or None,
                grid=grid_rect,
                step=step,
                block_size=block_size,
                start_frame=start_frame,
                end_frame=end_frame,
                start=start,
                end=end,
                sample_every=sample_every,
                sort=sort,  # type: ignore[arg-type]
                progress=update,
            )

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
