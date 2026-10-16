"""pixelprobe info：读取图片或视频基本信息。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import (
    INDEX_DIR_OPT,
    JSON_OPT,
    NO_INDEX_OPT,
    NO_PROGRESS_OPT,
    QUIET_OPT,
    VERBOSE_OPT,
    CliContext,
    cli_guard,
    find_index,
    run_capability,
    with_refs,
)
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv


def info(
    media: Path = typer.Argument(..., help="图片或视频路径"),
    no_index: bool = NO_INDEX_OPT,
    index_dir: Optional[Path] = INDEX_DIR_OPT,
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """读取图片或视频的基本信息（尺寸、帧率、总帧数、编码等）。

    图片另给出通道语义、调色板与透明度分析和提示标志（需完整读取一次图片，
    超大图片会慢一些）；视频另给出索引状态。
    """
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("info", ctx):
        outcome = run_capability(
            "info", media, None, None,
            index=find_index(media, no_index=no_index, index_dir=index_dir),
        )
        data = with_refs(outcome.data, outcome)
        if ctx.json_mode:
            json_writer.print_success("info", data)
            return
        media_info = MediaInfo.model_validate(data)
        if ctx.quiet:
            out_console.print(
                f"{media_info.media_type} {media_info.width}x{media_info.height}",
                highlight=False,
            )
            return
        rows: list[tuple[str, object]] = [
            ("路径", media_info.path),
            ("类型", media_info.media_type),
            ("宽度", media_info.width),
            ("高度", media_info.height),
            ("通道数", media_info.channels),
            ("文件大小(字节)", media_info.file_size_bytes),
        ]
        if media_info.media_type == "image":
            rows += [
                ("颜色模式", media_info.color_mode),
                ("格式", media_info.codec),
            ]
            rows += [(f"提示 {flag['code']}", flag["message"]) for flag in data.get("flags", [])]
        else:
            estimated = "（估算）" if media_info.frame_count_estimated else ""
            rows += [
                ("编码格式", media_info.codec),
                ("帧率", media_info.fps),
                ("总帧数", f"{media_info.frame_count}{estimated}"),
                ("时长(秒)", media_info.duration_seconds),
                ("像素格式", media_info.pixel_format),
                ("可变帧率", media_info.is_vfr),
                ("时间基", media_info.time_base),
                ("索引", "已建立" if data.get("index", {}).get("status") == "ready" else "未建立"),
            ]
        print_kv("媒体信息", rows)
