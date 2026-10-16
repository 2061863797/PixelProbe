"""pixelprobe evidence：生成可追溯的图片证据结果包。"""

from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import (
    JSON_OPT, QUIET_OPT, VERBOSE_OPT, NO_PROGRESS_OPT, CliContext, cli_guard, run_capability,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console, print_kv, progress_bar


def evidence(
    media: Path = typer.Argument(..., help="待分析图片路径"),
    output: Optional[Path] = typer.Option(
        None, "--output", help="证据 Bundle 输出目录",
    ),
    jpeg_quality: int = typer.Option(
        90, "--jpeg-quality", min=1, max=100, help="ELA 重压缩质量",
    ),
    block_size: int = typer.Option(
        32, "--block-size", min=8, max=512, help="噪声与边缘候选分块尺寸",
    ),
    c2pa_trust_anchors: Optional[Path] = typer.Option(
        None, "--c2pa-trust-anchors", help="替换 SDK 默认信任锚的 PEM 文件，最大 64 KiB",
    ),
    json_mode: bool = JSON_OPT,
    quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT,
    no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """提取元数据、验证 C2PA，并生成 ELA、噪声与边缘候选热力图。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("evidence", ctx):
        trust_pem = None
        if c2pa_trust_anchors is not None:
            from pixelprobe.c2pa_adapter import MAX_TRUST_BYTES
            with c2pa_trust_anchors.open("rb") as stream:
                raw = stream.read(MAX_TRUST_BYTES + 1)
            if len(raw) > MAX_TRUST_BYTES:
                raise ValueError("C2PA 信任锚文件超过 64 KiB")
            trust_pem = raw.decode("utf-8")
        target = output or media.with_name(f"{media.stem}.evidence.bundle")
        with progress_bar("证据分析", 1, ctx.progress_disabled) as update:
            outcome = run_capability(
                "evidence", media, None, None,
                {"jpeg_quality": jpeg_quality, "block_size": block_size,
                 "c2pa_trust_anchors_pem": trust_pem},
                output_path=target, progress=update,
            )
        report = outcome.data
        if ctx.json_mode:
            json_writer.print_success("evidence", report)
            return
        bundle = outcome.artifacts["bundle"]
        if ctx.quiet:
            out_console.print(str(bundle.root), highlight=False)
            return
        print_kv("证据分析", [
            ("结果包", bundle.root),
            ("EXIF 条目", report["metadata"].get("exif_entry_count", 0)),
            ("C2PA", report["c2pa"]["verification_state"]),
            ("ELA", report["ela"]["status"]),
            ("噪声一致性", report["noise_consistency"]["status"]),
            ("边缘候选", report["edge_candidates"]["status"]),
            ("结论", "未自动判定真伪，请结合原图与多项证据复核"),
        ])
