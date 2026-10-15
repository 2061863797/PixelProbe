"""生成结果包或管理本机数据的能力：取证、组合流程、表示生成与校验、索引、诊断、缓存。

这些能力写入的位置（output_path、索引/缓存目录）由入口决定：命令行使用用户给出的路径，
MCP 只使用服务器配置的受控目录；参数模型中不包含任何路径。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from pixelprobe.capabilities.base import (
    AddressRule,
    Capability,
    Invocation,
    Outcome,
    register,
)
from pixelprobe.models.errors import AddressError, InvalidRangeError

_FRAME_SPAN = frozenset({"range", "all"})
DEFAULT_WORKFLOW_END_FRAME = 1799


# ---------------- evidence ----------------

class EvidenceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jpeg_quality: int = Field(default=90, ge=1, le=100, description="ELA 重新压缩质量")
    block_size: int = Field(default=32, ge=8, le=512, description="噪声与边缘分析的块大小")
    c2pa_trust_anchors_pem: Optional[str] = Field(
        default=None, min_length=1, max_length=65536,
        description="可选 PEM 根证书集合，替换 SDK 默认信任锚；不等同于媒体真实性背书",
    )


def _evidence(call: Invocation) -> Outcome:
    from pixelprobe.evidence import analyze_evidence
    options: EvidenceOptions = call.options  # type: ignore[assignment]
    if call.output_path is None:
        raise InvalidRangeError("证据分析需要结果包输出位置")
    result = analyze_evidence(
        call.media, output_path=call.output_path, jpeg_quality=options.jpeg_quality,
        block_size=options.block_size, c2pa_trust_anchors_pem=options.c2pa_trust_anchors_pem,
        cancellation=call.cancellation,
    )
    if call.progress is not None:
        call.progress(1, 1)
    warnings = ["证据工具只报告可复核线索和适用边界，不自动判定媒体真伪"]
    if result.report.get("c2pa", {}).get("status") == "unavailable":
        reason = result.report["c2pa"].get("reason", "验证能力不可用")
        warnings.append(f"C2PA 未完成检查：{reason}")
    return Outcome(data=dict(result.report), artifacts={"bundle": result.bundle}, warnings=warnings)


# ---------------- 组合流程 ----------------

class WorkflowStepOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: int = Field(default=10, ge=0, le=255, description="变化像素判定阈值")
    min_changed_ratio: float = Field(
        default=0.0, ge=0, le=1, allow_inf_nan=False, description="视为变化的最小变化像素比例",
    )


def _workflow(mode: str):
    def run(call: Invocation) -> Outcome:
        from pixelprobe.workflows import WorkflowOptions, run_workflow
        options: WorkflowStepOptions = call.options  # type: ignore[assignment]
        if call.output_path is None:
            raise InvalidRangeError("组合流程需要结果包输出位置")
        start, end = 0, DEFAULT_WORKFLOW_END_FRAME
        spec = call.address.time
        if spec is not None and spec.mode == "range":
            if spec.kind != "frame":
                raise AddressError("组合流程只接受帧号范围，例如 f=0..1799")
            first, last = spec.values
            start = 0 if first is None else int(first)
            end = DEFAULT_WORKFLOW_END_FRAME if last is None else int(last)
        workflow_options = WorkflowOptions(
            workflow=mode, start_frame=start, end_frame=end,
            sample_every=call.address.step or 1, threshold=options.threshold,
            min_changed_ratio=options.min_changed_ratio,
        )
        result = run_workflow(
            call.media, reference_path=call.reference, output_path=call.output_path,
            options=workflow_options, cancellation=call.cancellation,
        )
        return Outcome(
            data=dict(result.report), artifacts={"bundle": result.bundle},
            warnings=list(result.report.get("limitations", [])),
        )
    return run


# ---------------- 表示生成与校验 ----------------

class GenerateOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requests: list[dict[str, Any]] = Field(
        min_length=1, max_length=32,
        description="一个或多个 RepresentationRequest；source.uri 与输出位置由入口替换",
    )


def _generate(call: Invocation) -> Outcome:
    import pixelprobe
    from pixelprobe.domain.media import MediaSource
    from pixelprobe.engine.request import OutputRequest, RepresentationRequest
    options: GenerateOptions = call.options  # type: ignore[assignment]
    if call.output_path is None:
        raise InvalidRangeError("表示生成需要结果包输出位置")
    requests = []
    for item in options.requests:
        request = RepresentationRequest.model_validate(item)
        update: dict[str, Any] = {
            "source": MediaSource(
                source_id=request.source.source_id, kind="file",
                uri=str(call.media.resolve()),
                declared_media_type=request.source.declared_media_type,
            ),
            "output": OutputRequest(
                format="zarr" if request.output.format == "zarr" else "bundle",
                include_preview=request.output.include_preview,
                preview_config=request.output.preview_config,
                metadata_policy=request.output.metadata_policy,
            ),
        }
        if "resources" in call.settings:
            update["resources"] = call.settings["resources"]
        requests.append(request.model_copy(update=update))
    options_kw = {
        key: call.settings[key]
        for key in ("cache_root", "checkpoint_path", "resume_from") if key in call.settings
    }
    if call.cancellation is not None:
        options_kw["cancellation"] = call.cancellation
    result = pixelprobe.generate(tuple(requests), output_path=call.output_path, **options_kw)
    if result.bundle is None:
        raise RuntimeError("生成完成但没有返回 Bundle")
    return Outcome(
        data={
            "path": str(result.bundle.root),
            "bundle_id": result.bundle.manifest.bundle_id,
            "schema_version": result.bundle.manifest.schema_version,
            "plan_id": result.plan.plan_id,
            "decode_passes": result.decode_passes,
            "source_frames_decoded": getattr(result, "source_frames_decoded", None),
            "frames_converted": getattr(result, "frames_converted", None),
            "frames_stored": getattr(result, "frames_stored", None),
            "artifact_count": len(result.bundle.manifest.artifacts),
            "cache_hits": result.cache_hits,
            "cache_writes": result.cache_writes,
            "data_artifact_ids": [
                record.artifact_id for record in result.bundle.manifest.artifacts
                if record.kind == "data"
            ],
        },
        artifacts={"bundle": result.bundle},
    )


class ValidateOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata_only: bool = Field(default=False, description="只校验结构与大小，不重新计算 SHA-256")
    strict: bool = Field(default=False, description="把未知可选字段和未登记文件也视为错误")


def _validate(call: Invocation) -> Outcome:
    from pixelprobe.artifacts import BundleReader
    options: ValidateOptions = call.options  # type: ignore[assignment]
    result = BundleReader().open(
        call.media, verify="metadata" if options.metadata_only else "full", strict=options.strict,
    )
    return Outcome(data={
        "path": str(result.root),
        "bundle_id": result.manifest.bundle_id,
        "schema_version": result.manifest.schema_version,
        "artifact_count": len(result.manifest.artifacts),
        "verification": "metadata" if options.metadata_only else "full",
        "content_integrity_verified": not options.metadata_only,
        "strict": options.strict,
        "notices": list(result.notices),
    })


# ---------------- 索引 ----------------

class IndexBuildOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = Field(default=False, description="已有有效索引时也重新建立")


class IndexShowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: bool = Field(default=False, description="同时列出全部分段")


class IndexPruneOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    older_than_days: float = Field(default=30, ge=0, description="至少多少天未使用")
    all_entries: bool = Field(default=False, description="不论时间，选中全部索引")
    apply: bool = Field(default=False, description="执行删除；默认只预览")


def _store(call: Invocation):
    from pixelprobe.index import IndexStore
    return IndexStore(call.settings.get("index_dir"))


def _segment_refs(segments: list[dict]) -> dict[str, str]:
    return {
        f"seg{n}": f"f={item['start_frame']}..{item['end_frame']}"
        for n, item in enumerate(segments, start=1)
    }


def _index_build(call: Invocation) -> Outcome:
    from pixelprobe.index import build_index
    options: IndexBuildOptions = call.options  # type: ignore[assignment]
    store = _store(call)
    existing = None if options.force else store.lookup(call.media)
    if existing is not None:
        index, status = existing, "existing"
    else:
        from pixelprobe.index.builder import DEFAULT_THUMB_BUDGET_BYTES
        cancel = call.cancellation.raise_if_cancelled if call.cancellation is not None else None
        built = build_index(
            call.media, progress=call.progress, cancel_check=cancel,
            thumb_budget_bytes=call.settings.get("thumb_budget", DEFAULT_THUMB_BUDGET_BYTES),
        )
        index, status = store.save(call.media, built), "built"
    return Outcome(
        data={"status": status, **index.summary(), "index_dir": str(store.root)},
        refs=_segment_refs(index.segments),
    )


def _index_show(call: Invocation) -> Outcome:
    options: IndexShowOptions = call.options  # type: ignore[assignment]
    store = _store(call)
    index = store.lookup(call.media)
    if index is None:
        return Outcome(data={"status": "missing", "index_dir": str(store.root)})
    data: dict[str, Any] = {"status": "ready", **index.summary(), "index_dir": str(store.root)}
    if options.segments:
        data["segments"] = index.segments
    return Outcome(data=data, refs=_segment_refs(index.segments))


def _index_remove(call: Invocation) -> Outcome:
    store = _store(call)
    removed = store.remove(call.media)
    return Outcome(data={"status": "removed" if removed else "missing", "index_dir": str(store.root)})


def _index_prune(call: Invocation) -> Outcome:
    options: IndexPruneOptions = call.options  # type: ignore[assignment]
    report = _store(call).prune(
        max_age_seconds=None if options.all_entries else options.older_than_days * 86400,
        dry_run=not options.apply,
    )
    report["status"] = "pruned" if options.apply else "preview"
    return Outcome(data=report)


# ---------------- 诊断与缓存 ----------------

def _doctor(call: Invocation) -> Outcome:
    from pixelprobe.cli.doctor_cmd import inspect_environment
    return Outcome(data=inspect_environment(
        call.settings.get("allowed_roots"), call.settings.get("artifact_root"),
    ))


class CacheOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["inspect", "prune", "clear"] = Field(default="inspect")
    older_than_days: float = Field(default=7, ge=0, description="prune：至少多少天未修改")
    apply: bool = Field(default=False, description="prune：执行删除；默认只预览")


def _cache(call: Invocation) -> Outcome:
    from pixelprobe.cli.cache_cmd import default_cache_root
    from pixelprobe.engine.cache import LocalArrayCache
    options: CacheOptions = call.options  # type: ignore[assignment]
    root = call.settings.get("cache_dir") or default_cache_root()
    cache = LocalArrayCache(root)
    if options.action == "clear":
        return Outcome(data={"cache_dir": str(root), "removed_entries": cache.clear()})
    if options.action == "prune":
        return Outcome(data=cache.maintain(
            dry_run=not options.apply, max_age_seconds=options.older_than_days * 86400,
        ))
    return Outcome(data=cache.maintain())


EVIDENCE = register(Capability(
    name="evidence", summary="图片取证线索：EXIF、C2PA、ELA、噪声与边缘候选（不判定真伪）",
    run=_evidence, cli="evidence", mcp_tool="pixelprobe_evidence",
    options=EvidenceOptions, writes_bundle=True,
))
FIRST_CHANGE = register(Capability(
    name="first_change", summary="组合流程：在帧范围内定位第一次显著变化，结果写入结果包",
    run=_workflow("first_change"), cli="workflow --mode first_change",
    mcp_tool="pixelprobe_find", mcp_selector="first_change",
    address=AddressRule(_FRAME_SPAN, step=True), options=WorkflowStepOptions,
    writes_bundle=True,
))
COMPARE_MEDIA = register(Capability(
    name="compare_media", summary="组合流程：逐帧比较两个媒体版本，结果写入结果包",
    run=_workflow("compare_media"), cli="workflow --mode compare_media",
    mcp_tool="pixelprobe_compare", mcp_selector="media",
    address=AddressRule(_FRAME_SPAN, step=True), options=WorkflowStepOptions,
    writes_bundle=True,
))
QUALITY_OVERVIEW = register(Capability(
    name="quality_overview", summary="组合流程：画面质量概览，结果写入结果包",
    run=_workflow("quality_overview"), cli="workflow --mode quality_overview",
    mcp_tool="pixelprobe_overview", mcp_selector="quality",
    address=AddressRule(_FRAME_SPAN, step=True), options=WorkflowStepOptions,
    writes_bundle=True,
))
GENERATE = register(Capability(
    name="generate", summary="执行 RepresentationRequest，生成可校验的正式结果包",
    run=_generate, cli="generate", mcp_tool="pixelprobe_bundle", mcp_selector="generate",
    options=GenerateOptions, writes_bundle=True,
))
VALIDATE = register(Capability(
    name="validate", summary="校验结果包的结构、大小与 SHA-256",
    run=_validate, cli="validate", mcp_tool="pixelprobe_bundle", mcp_selector="validate",
    options=ValidateOptions,
))
INDEX_BUILD = register(Capability(
    name="index_build", summary="为视频建立分层索引（一次顺序解码）",
    run=_index_build, cli="index build", mcp_tool="pixelprobe_index", mcp_selector="build",
    options=IndexBuildOptions,
))
INDEX_SHOW = register(Capability(
    name="index_show", summary="查看视频当前内容对应的索引与分段",
    run=_index_show, cli="index show", mcp_tool="pixelprobe_index", mcp_selector="show",
    options=IndexShowOptions,
))
INDEX_REMOVE = register(Capability(
    name="index_remove", summary="删除视频当前内容对应的索引",
    run=_index_remove, cli="index remove", mcp_tool="pixelprobe_index", mcp_selector="remove",
))
INDEX_PRUNE = register(Capability(
    name="index_prune", summary="清理长时间未使用的索引（默认只预览）",
    run=_index_prune, cli="index prune", mcp_tool="pixelprobe_index", mcp_selector="prune",
    options=IndexPruneOptions, needs_media=False,
))
DOCTOR = register(Capability(
    name="doctor", summary="检查运行环境、可选依赖、MCP 配置与结果目录写入权限",
    run=_doctor, cli="doctor", mcp_tool="pixelprobe_system", mcp_selector="doctor",
    needs_media=False,
))
CACHE = register(Capability(
    name="cache", summary="查看或清理本机执行缓存（默认只预览）",
    run=_cache, cli="cache", mcp_tool="pixelprobe_system", mcp_selector="cache",
    options=CacheOptions, needs_media=False,
))
