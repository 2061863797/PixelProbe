"""组合分析 CLI。"""
from pathlib import Path
from typing import Optional

import typer

from pixelprobe.cli import CliContext, JSON_OPT, QUIET_OPT, VERBOSE_OPT, NO_PROGRESS_OPT, cli_guard
from pixelprobe.output import json_writer
from pixelprobe.output.console import print_kv, out_console


def workflow(
    media: Path = typer.Argument(..., help="主媒体路径"),
    mode: str = typer.Option(..., "--mode", help="first_change / compare_media / quality_overview"),
    reference: Optional[Path] = typer.Option(None, "--reference", help="比较的第二个媒体"),
    output: Optional[Path] = typer.Option(None, "--output", help="新的结果包目录"),
    start_frame: int = typer.Option(0, "--start-frame", min=0),
    end_frame: int = typer.Option(1799, "--end-frame", min=0, help="结束展示帧，包含该帧"),
    sample_every: int = typer.Option(1, "--sample-every", min=1),
    threshold: int = typer.Option(10, "--threshold", min=0, max=255),
    min_changed_ratio: float = typer.Option(0.0, "--min-changed-ratio", min=0, max=1),
    json_mode: bool = JSON_OPT, quiet: bool = QUIET_OPT,
    verbose: bool = VERBOSE_OPT, no_progress: bool = NO_PROGRESS_OPT,
) -> None:
    """运行有界组合分析；采样与未检查范围会保存在报告中。"""
    from pixelprobe.workflows import WorkflowOptions, run_workflow
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("workflow", ctx):
        options = WorkflowOptions(workflow=mode, start_frame=start_frame, end_frame=end_frame,
                                  sample_every=sample_every, threshold=threshold,
                                  min_changed_ratio=min_changed_ratio)
        result = run_workflow(media, reference_path=reference, options=options,
                              output_path=output or media.with_name(f"{media.stem}.{mode}.bundle"))
        if ctx.json_mode:
            json_writer.print_success("workflow", result.report)
        elif ctx.quiet:
            out_console.print(str(result.bundle.root), highlight=False)
        else:
            print_kv("组合分析", [("流程", mode), ("记录数", result.report["records"]),
                                  ("结束原因", result.report["stop_reason"]),
                                  ("结果包", result.bundle.root),
                                  ("覆盖范围", "仅报告中的帧范围与采样点")])
