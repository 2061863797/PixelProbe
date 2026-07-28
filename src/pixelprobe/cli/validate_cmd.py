"""pixelprobe validate：完整校验正式 Bundle。"""

from __future__ import annotations

from pathlib import Path

import typer

from pixelprobe.artifacts import BundleReader
from pixelprobe.cli import JSON_OPT, CliContext, cli_guard
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console


def validate(
    bundle: Path = typer.Argument(..., exists=True, file_okay=False, help="Bundle 目录"),
    metadata_only: bool = typer.Option(
        False, "--metadata-only", help="只校验 Schema 与路径，不宣称内容完整",
    ),
    strict: bool = typer.Option(
        False, "--strict", help="把未知可选字段和未登记文件也视为错误",
    ),
    json_mode: bool = JSON_OPT,
) -> None:
    """默认验证全部登记文件的大小、SHA-256、NPY 头和引用。"""
    ctx = CliContext(json_mode=json_mode, no_progress=True)
    with cli_guard("validate", ctx):
        result = BundleReader().open(
            bundle,
            verify="metadata" if metadata_only else "full",
            strict=strict,
        )
        data = {
            "path": str(result.root),
            "bundle_id": result.manifest.bundle_id,
            "schema_version": result.manifest.schema_version,
            "artifact_count": len(result.manifest.artifacts),
            "verification": "metadata" if metadata_only else "full",
            "content_integrity_verified": not metadata_only,
            "strict": strict,
            "notices": list(result.notices),
        }
        if json_mode:
            json_writer.print_success("validate", data)
        else:
            label = "元数据校验通过" if metadata_only else "完整校验通过"
            out_console.print(f"{label}：{result.root}", highlight=False)
