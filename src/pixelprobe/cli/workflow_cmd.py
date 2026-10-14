"""组合分析 CLI：三种流程分别对应能力 first_change、compare_media、quality_overview。"""
from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import (
    AT_OPT, REF_FILE_OPT, CliContext, JSON_OPT, QUIET_OPT, VERBOSE_OPT, NO_PROGRESS_OPT,
    cli_guard, run_capability,
)
from pixelprobe.output import json_writer
from pixelprobe.output.console import print_kv, out_console

MODES = ("first_change", "compare_media", "quality_overview")


def workflow(
    media: Path = typer.Argument(..., help="主媒体路径"),
    mode: str = typer.Option(..., "--mode", help="first_change / compare_media / quality_overview"),
    reference: Optional[Path] = typer.Option(None, "--reference", help="比较的第二个媒体"),
    output: Optional[Path] = typer.Option(None, "--output", help="新的结果包目录"),
    at: Optional[str] = AT_OPT,
    ref_file: Optional[Path] = REF_FILE_OPT,
    threshold: int = typer.Option(10, "--threshold", min=0, max=255),
    min_changed_ratio: float = typer.Option(0.0, "--min-changed-ratio", min=0, max=1),
    json_mode: bool = JSON_OPT, quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT, no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """运行有界组合分析：--at "f=0..1799 step=2"（只接受帧号，缺省 0～1799 帧）；采样与未检查范围保存在报告中。"""
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("workflow", ctx):
        if mode not in MODES:
            raise typer.BadParameter(f"--mode {mode} 无效，可选 {' / '.join(MODES)}")
        outcome = run_capability(
            mode, media, at, ref_file,
            {"threshold": threshold, "min_changed_ratio": min_changed_ratio},
            reference=reference,
            output_path=output or media.with_name(f"{media.stem}.{mode}.bundle"),
        )
        bundle = outcome.artifacts["bundle"]
        if ctx.json_mode:
            json_writer.print_success("workflow", outcome.data)
        elif ctx.quiet:
            out_console.print(str(bundle.root), highlight=False)
        else:
            print_kv("组合分析", [("流程", mode), ("记录数", outcome.data["records"]),
                                  ("结束原因", outcome.data["stop_reason"]),
                                  ("结果包", bundle.root),
                                  ("覆盖范围", "仅报告中的帧范围与采样点")])
