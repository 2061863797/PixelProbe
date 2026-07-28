"""pixelprobe generate：执行统一 RepresentationRequest。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from pydantic import ValidationError

import pixelprobe
from pixelprobe.cli import JSON_OPT, CliContext, cli_guard
from pixelprobe.domain.media import MediaSource
from pixelprobe.engine.request import OutputRequest, RepresentationRequest
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.output import json_writer
from pixelprobe.output.console import out_console


def generate(
    media: Path = typer.Argument(..., exists=True, dir_okay=False, help="图片或视频路径"),
    request_file: Path = typer.Option(
        ..., "--request", exists=True, dir_okay=False,
        help="RepresentationRequest JSON 文件",
    ),
    output: Path = typer.Option(..., "--output", help="输出 .bundle 目录"),
    cache_dir: Path | None = typer.Option(
        None, "--cache-dir", help="可选的本机内容缓存目录",
    ),
    checkpoint: Path | None = typer.Option(
        None, "--checkpoint", help="持续写入的恢复检查点",
    ),
    resume_from: Path | None = typer.Option(
        None, "--resume-from", exists=True, dir_okay=False,
        help="从严格匹配的检查点恢复",
    ),
    json_mode: bool = JSON_OPT,
) -> None:
    """通过统一 DAG 生成一个或多个正式媒体表示。"""
    ctx = CliContext(json_mode=json_mode, no_progress=True)
    with cli_guard("generate", ctx):
        if (checkpoint is not None or resume_from is not None) and cache_dir is None:
            raise InvalidRangeError("--checkpoint/--resume-from 必须同时提供 --cache-dir")
        try:
            raw = json.loads(request_file.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else [raw]
            requests = []
            for item in items:
                request = RepresentationRequest.model_validate(item)
                request = request.model_copy(update={
                    "source": MediaSource(
                        source_id=request.source.source_id,
                        kind="file",
                        uri=str(media.resolve()),
                        declared_media_type=request.source.declared_media_type,
                    ),
                    "output": OutputRequest(
                        format="bundle",
                        include_preview=request.output.include_preview,
                        preview_config=request.output.preview_config,
                        metadata_policy=request.output.metadata_policy,
                    ),
                })
                requests.append(request)
        except (OSError, json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise InvalidRangeError(f"请求文件无效：{exc}") from exc
        result = pixelprobe.generate(
            tuple(requests), output_path=output, cache_root=cache_dir,
            checkpoint_path=checkpoint, resume_from=resume_from,
        )
        assert result.bundle is not None
        data = {
            "path": str(result.bundle.root),
            "bundle_id": result.bundle.manifest.bundle_id,
            "schema_version": result.bundle.manifest.schema_version,
            "plan_id": result.plan.plan_id,
            "decode_passes": result.decode_passes,
            "artifact_count": len(result.bundle.manifest.artifacts),
            "cache_hits": result.cache_hits,
            "cache_writes": result.cache_writes,
        }
        if json_mode:
            json_writer.print_success("generate", data)
        else:
            out_console.print(f"已生成：{result.bundle.root}", highlight=False)
            out_console.print(
                f"计划 {result.plan.plan_id}，解码 {result.decode_passes} 次，"
                f"Artifact {len(result.bundle.manifest.artifacts)} 个",
                highlight=False,
            )
