"""pixelprobe changes：计算相邻帧变化量，返回变化最大的帧。"""

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
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.output import csv_writer, json_writer
from pixelprobe.output.console import out_console, print_table, progress_bar
from pixelprobe.output.image_writer import save_png
from pixelprobe.output.plot import render_curve
from pixelprobe.utils.coordinates import parse_point, parse_rect


def changes(
    media: Path = typer.Argument(..., help="视频路径"),
    point: Optional[str] = typer.Option(
        None, "--point", help="单像素坐标 x,y"
    ),
    rect: Optional[str] = typer.Option(
        None, "--rect", help="矩形区域 x,y,width,height"
    ),
    grid: Optional[str] = typer.Option(
        None, "--grid", help="网格采样区域 x,y,width,height"
    ),
    step: Optional[int] = typer.Option(
        None, "--step", help="网格采样步长（与 --grid 搭配）"
    ),
    top: int = typer.Option(10, "--top", help="返回变化最大的前 N 帧"),
    threshold: Optional[float] = typer.Option(
        None, "--threshold",
        help="事件分段阈值（作用于归一化得分；缺省自动取 mean + 3*std）",
    ),
    curve_image: Optional[Path] = typer.Option(
        None, "--curve-image", help="导出完整变化曲线 PNG（事件区间描色）"
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
    csv: Optional[Path] = typer.Option(
        None, "--csv", help="导出全部变化记录到 CSV"
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """计算相邻帧变化量并按阈值分段为事件区间。

    --point / --rect / --grid 最多指定一个，都不给时默认整帧。
    """
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("changes", ctx):
        if top < 1:
            raise InvalidRangeError(f"--top {top} 无效，必须 >= 1")
        point_tuple = parse_point(point) if point else None
        rect_tuple = parse_rect(rect) if rect else None
        grid_tuple = parse_rect(grid) if grid else None

        with progress_bar("变化检测", 1, ctx.progress_disabled) as update:
            result = core.detect_changes(
                media,
                point=point_tuple,
                rect=rect_tuple,
                grid=grid_tuple,
                step=step,
                start_frame=start_frame,
                end_frame=end_frame,
                start=start,
                end=end,
                sample_every=sample_every,
                progress=update,
            )
        top_records = core.top_changes(result.records, top)
        events, threshold_used = core.segment_events(
            result.records, threshold=threshold
        )

        csv_path: str | None = None
        if csv is not None:
            csv_writer.write_changes_csv(csv, result.records)
            csv_path = str(csv)

        curve_path: str | None = None
        if curve_image is not None:
            frame_of = {r.frame: i for i, r in enumerate(result.records)}
            prev_of = {r.previous_frame: i for i, r in enumerate(result.records)}
            spans = [
                (prev_of[e.start_frame], frame_of[e.end_frame])
                for e in events
                if e.start_frame in prev_of and e.end_frame in frame_of
            ]
            curve = render_curve(
                [r.normalized_score for r in result.records],
                spans=spans, y_min=0.0,
            )
            save_png(curve, curve_image)
            curve_path = str(curve_image)

        data = {
            "mode": result.mode,
            "point": (
                {"x": point_tuple[0], "y": point_tuple[1]}
                if point_tuple else None
            ),
            "rect": (
                {"x": rect_tuple[0], "y": rect_tuple[1],
                 "width": rect_tuple[2], "height": rect_tuple[3]}
                if rect_tuple else None
            ),
            "grid": (
                {"x": grid_tuple[0], "y": grid_tuple[1],
                 "width": grid_tuple[2], "height": grid_tuple[3],
                 "step": step if step is not None else 1}
                if grid_tuple else None
            ),
            "start_frame": result.frame_range.start,
            "end_frame": result.frame_range.end,
            "sample_every": result.frame_range.sample_every,
            "frames_analyzed": result.frames_analyzed,
            "top": [r.to_dict() for r in top_records],
            "events": [e.to_dict() for e in events],
            "event_threshold_used": threshold_used,
            "csv_path": csv_path,
            "curve_image_path": curve_path,
        }
        if ctx.json_mode:
            json_writer.print_success("changes", data)
            return
        if ctx.quiet:
            for r in top_records:
                out_console.print(f"{r.frame} {r.score}", highlight=False)
            return
        print_table(
            f"变化最大的 {len(top_records)} 帧（{result.mode} 模式）",
            ["帧号", "前一帧", "时间(秒)", "得分", "归一化得分"],
            [
                [r.frame, r.previous_frame, r.time_seconds,
                 r.score, r.normalized_score]
                for r in top_records
            ],
        )
        if events:
            print_table(
                f"事件区间（阈值 {threshold_used}）",
                ["起始帧", "结束帧", "起始(秒)", "结束(秒)", "峰值帧", "峰值得分"],
                [
                    [e.start_frame, e.end_frame, e.start_time, e.end_time,
                     e.peak_frame, e.peak_score]
                    for e in events
                ],
            )
        else:
            out_console.print(
                f"未检出事件区间（阈值 {threshold_used}）", highlight=False
            )
        if csv_path:
            out_console.print(f"全部记录已导出：{csv_path}", highlight=False)
        if curve_path:
            out_console.print(f"变化曲线已导出：{curve_path}", highlight=False)
