"""组合分析 CLI。"""
from pathlib import Path
from typing import Optional

import typer

from pixelprobe.address import check
from pixelprobe.cli import (
    AT_OPT, REF_FILE_OPT, CliContext, JSON_OPT, QUIET_OPT, VERBOSE_OPT, NO_PROGRESS_OPT,
    cli_guard, read_address,
)
from pixelprobe.models.errors import AddressError
from pixelprobe.output import json_writer
from pixelprobe.output.console import print_kv, out_console

DEFAULT_START_FRAME = 0
DEFAULT_END_FRAME = 1799


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
    from pixelprobe.workflows import WorkflowOptions, run_workflow
    ctx = CliContext(json_mode, quiet, verbose, no_progress)
    with cli_guard("workflow", ctx):
        address = read_address(at, ref_file)
        check(address, command="workflow", time_modes={"range", "all"}, step=True)
        start_frame, end_frame = DEFAULT_START_FRAME, DEFAULT_END_FRAME
        if address.time is not None and address.time.mode == "range":
            if address.time.kind != "frame":
                raise AddressError("workflow 只接受帧号范围，例如 f=0..1799")
            first, last = address.time.values
            start_frame = DEFAULT_START_FRAME if first is None else int(first)
            end_frame = DEFAULT_END_FRAME if last is None else int(last)
        options = WorkflowOptions(workflow=mode, start_frame=start_frame, end_frame=end_frame,
                                  sample_every=address.step or 1, threshold=threshold,
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
