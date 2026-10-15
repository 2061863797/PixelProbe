"""pixelprobe streams：视频容器与编码结构（取证与质量排查线索）。"""

from __future__ import annotations

from pathlib import Path

import typer

from pixelprobe.cli import (
    JSON_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    run_capability,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar


def streams(
    media: Path = typer.Argument(..., help="视频路径"),
    frame_types: bool = typer.Option(
        False, "--frame-types", help="再顺序解码一遍统计 I/P/B 帧类型（较慢）",
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """读取容器标签、各流参数、关键帧间隔（GOP）、时间戳空隙与每秒码率，并列出可复核线索。

    缺省只读压缩包、不解码画面。线索只用于复核，不判定是否重新编码、剪辑或伪造。
    """
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("streams", ctx):
        with progress_bar("码流结构", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "streams", media, None, None, {"frame_types": frame_types}, progress=update,
            )
        data = outcome.data
        if ctx.json_mode:
            json_writer.print_success("streams", data)
            return
        packets = data["video_packets"]
        if ctx.quiet:
            out_console.print(f"{packets['keyframe_count']} keyframes {len(data['clues'])} clues",
                              highlight=False)
            return
        video = next(s for s in data["streams"] if s["index"] == data["video_stream_index"])
        gop = packets["gop_length_stats"]
        rows: list[tuple[str, object]] = [
            ("容器", f"{data['container']['format']}（{data['container']['format_long_name']}）"),
            ("视频流", f"{video['codec']} {video['profile'] or ''} {video['width']}x{video['height']} "
                      f"{video['pix_fmt']}"),
            ("帧率", f"平均 {video['average_rate']} / 标称 {video['base_rate']}"),
            ("包 / 关键帧", f"{packets['packet_count']} / {packets['keyframe_count']}"),
            ("GOP 长度", "—" if gop is None else
             f"{gop['min']:g}～{gop['max']:g}（中位 {gop['median']:g}，"
             f"{'固定' if packets['regular_gop'] else '不固定'}）"),
            ("B 帧（推断）", "有" if packets["b_frames_inferred"] else "无"),
            ("时间戳空隙", len(packets["timestamp_gaps"])),
        ]
        if data["frame_types"]:
            rows.append(("帧类型", data["frame_types"]["counts"]))
        for clue in data["clues"]:
            rows.append((clue["code"], clue["message"]))
        print_kv("码流结构", rows)
