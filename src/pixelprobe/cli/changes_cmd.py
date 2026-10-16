"""pixelprobe changes：计算相邻帧变化量，返回变化最大的帧。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import (
    AT_OPT,
    INDEX_DIR_OPT,
    JSON_OPT,
    NO_INDEX_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    REF_FILE_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    find_index,
    run_capability,
    with_refs,
)
from pixelprobe.output import csv_writer, json_writer
from pixelprobe.output.console import out_console, print_table, progress_bar
from pixelprobe.output.image_writer import save_png


def changes(
    media: Path = typer.Argument(..., help="视频路径"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    top: int = typer.Option(10, "--top", help="返回变化最大的前 N 帧"),
    threshold: Optional[float] = typer.Option(
        None, "--threshold",
        help="事件分段阈值（作用于归一化得分；缺省自动取剔除最大记录后的 mean + 3*std）",
    ),
    curve_image: Optional[Path] = typer.Option(
        None, "--curve-image", help="导出完整变化曲线 PNG（事件区间描色）"
    ),
    csv: Optional[Path] = typer.Option(
        None, "--csv", help="导出全部变化记录到 CSV"
    ),
    no_index: bool = NO_INDEX_OPT,
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """计算相邻帧变化量并按阈值分段为事件区间。

    --at 中 point(...) / rect(...) / cell(...) / grid(...) 最多一个，都不给时默认整帧；
    可加时间范围与 step，例如 --at "t=1s..5s step=2 rect(400,200,200,150)"。
    """
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("changes", ctx):
        with progress_bar("变化检测", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "changes", media, at, ref_file, {"top": top, "threshold": threshold},
                progress=update,
                index=find_index(media, no_index=no_index, index_dir=index_dir),
            )
        data = dict(outcome.data)
        csv_path: str | None = None
        if csv is not None:
            csv_writer.write_changes_csv(csv, outcome.artifacts["records"])
            csv_path = str(csv)
        curve_path: str | None = None
        if curve_image is not None:
            save_png(outcome.images["curve"](), curve_image)
            curve_path = str(curve_image)
        data.update(csv_path=csv_path, curve_image_path=curve_path)
        with_refs(data, outcome)
        if ctx.json_mode:
            json_writer.print_success("changes", data)
            return
        top_records = data["top"]
        if ctx.quiet:
            for r in top_records:
                out_console.print(f"{r['frame']} {r['score']}", highlight=False)
            return
        print_table(
            f"变化最大的 {len(top_records)} 帧（{data['mode']} 模式）",
            ["帧号", "前一帧", "时间(秒)", "得分", "归一化得分"],
            [
                [r["frame"], r["previous_frame"], r["time_seconds"],
                 r["score"], r["normalized_score"]]
                for r in top_records
            ],
        )
        events = data["events"]
        threshold_used = data["event_threshold_used"]
        if events:
            print_table(
                f"事件区间（阈值 {threshold_used}）",
                ["编号", "起始帧", "结束帧", "起始(秒)", "结束(秒)", "峰值帧", "峰值得分"],
                [
                    [f"@event{n}", e["start_frame"], e["end_frame"], e["start_time"],
                     e["end_time"], e["peak_frame"], e["peak_score"]]
                    for n, e in enumerate(events, start=1)
                ],
            )
        else:
            out_console.print(f"未检出事件区间（阈值 {threshold_used}）", highlight=False)
        if csv_path:
            out_console.print(f"全部记录已导出：{csv_path}", highlight=False)
        if curve_path:
            out_console.print(f"变化曲线已导出：{curve_path}", highlight=False)
