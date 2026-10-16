"""只读取媒体头的执行预算；展示帧使用顺序解码成本，绝不按平均帧率换算。"""
from __future__ import annotations

from pixelprobe.utils.resource_budget import EVIDENCE_BYTES_PER_PIXEL

MIB = 1024 * 1024
RUNTIME_BYTES = 128 * MIB
CONTROL_BYTES = 4 * MIB


def media_plan(operation, info, payload):
    pixels = info.width * info.height
    decoded, stored, full_scan = 1, 1, False
    # 原生解码、RGB 转换、数组副本及算子浮点工作区。
    # evidence 用实测峰值校准的系数；inspect_media 尚未实测，保持原保守值
    per_pixel = {"evidence": EVIDENCE_BYTES_PER_PIXEL, "inspect_media": 128}.get(operation, 32)
    memory = pixels * per_pixel
    disk = 0
    if operation == "generate_representation":
        from pixelprobe.engine.request import RepresentationRequest
        request = RepresentationRequest.model_validate(payload["request"])
        selection = request.selection
        if info.media_type == "video":
            if selection.mode == "frame_interval":
                decoded = selection.requested_end_frame_exclusive
                span = decoded - selection.requested_start_frame
                stored = (span + selection.sample_every - 1) // selection.sample_every
            elif selection.mode == "indices":
                # 即便最后一个请求帧因抽样未保存，也必须确认原始边界有效。
                decoded = selection.requested_indices[-1] + 1
                stored = len(selection.requested_indices[::selection.sample_every])
            else:
                decoded = info.frame_count
                full_scan = True
                stored = (decoded + selection.sample_every - 1) // selection.sample_every if decoded else None
                if selection.mode == "time_interval":
                    stored = decoded
        if stored is None:
            return {"estimate_kind": "unknown_frame_count", "full_scan": True,
                    "decoded_frames_upper_bound": None, "stored_frames_estimate": None}
        feature = request.feature.name
        # frames/颜色/光流可能保留全分辨率浮点结果。切片按实际几何维度估算。
        if request.representation == "xt":
            samples = info.width
        elif request.representation == "yt":
            samples = info.height
        elif request.representation == "roi_t":
            samples = 1
        elif request.representation in {"points_t", "path_t"}:
            samples = int(request.feature.config.get("sample_count",
                          request.feature.config.get("point_count", pixels)))
        else:
            samples = pixels
        values = stored * samples * (3 if feature == "rgb" else 32)
        # 帧 mmap、缓存复制、持久化副本和 PNG/元数据均计入；运行时按真实编码字节复核。
        frames = stored * pixels * 3 + (pixels * 8 if info.media_type == "image" else 0)
        disk = frames + values * 4 + 4 * MIB
        memory += values * (4 if request.reduction or feature != "rgb" else 2)
        if request.output.include_preview:
            memory += values * 8
        if feature in {"flow", "farneback"}:
            memory += pixels * 128
        if "fft" in feature or "frequency" in feature or feature == "stft":
            memory += values * 16
    elif operation == "evidence":
        disk = pixels * 48 + 4 * MIB
    elif operation in {"find_changes", "inspect_media"} and info.media_type == "video":
        if operation == "inspect_media" and payload.get("detail", "quick") == "quick":
            decoded = 0
            full_scan = False
        elif operation == "find_changes" and payload.get("end_frame") is not None:
            decoded = payload["end_frame"] + 1
        else:
            decoded = info.frame_count
            full_scan = True
        # 保留逐帧统计、PTS 和变化记录的对象开销。
        memory += (decoded if decoded is not None else 3600) * 4096
    elif info.media_type == "video":
        full_scan = payload.get("time_seconds") is not None
        decoded = info.frame_count if full_scan else (payload.get("frame") or 0) + 1
        if full_scan:
            memory += (decoded or 3600) * 4096
    return {
        "estimate_kind": "conservative_execution_plan",
        "decoded_frames_upper_bound": None if full_scan else decoded,
        "decoded_frames_per_pass_estimate": decoded,
        "frame_count_estimated": bool(getattr(info, "frame_count_estimated", False)),
        "decode_cost_scope": "single_shared_store_pass" if operation == "generate_representation"
                             else "per_pass_additional_scans_possible",
        "stored_frames_estimate": stored,
        "full_scan": full_scan,
        "full_resolution_pixels": pixels,
        "analysis_memory_bytes": memory,
        "memory_bytes": RUNTIME_BYTES + max(16 * MIB, memory),
        "temporary_bytes": max(16 * MIB, disk) if disk else 0,
        "runtime_allowance_bytes": RUNTIME_BYTES,
    }


LIGHT_CAPABILITIES = {
    "info", "validate", "index_show", "index_remove", "index_prune", "doctor", "cache",
}
SINGLE_FRAME_CAPABILITIES = {"frame", "pixel", "region", "compare"}
SEQUENTIAL_CAPABILITIES = {"changes", "scan", "sheet"}
WORKFLOW_CAPABILITIES = {"first_change", "compare_media", "quality_overview"}
MCP_INDEX_THUMB_BUDGET_BYTES = 48 * MIB


def _light_plan() -> dict:
    return {
        "estimate_kind": "light_metadata_operation", "full_scan": False,
        "decoded_frames_upper_bound": 0, "stored_frames_estimate": 0,
        "analysis_memory_bytes": 16 * MIB,
        "memory_bytes": RUNTIME_BYTES + 16 * MIB,
        "temporary_bytes": 0,
        "runtime_allowance_bytes": RUNTIME_BYTES,
    }


def _end_frame(address, info) -> tuple[int | None, bool]:
    """按地址估算需要顺序解码到的展示帧数；时间范围或全片保守按整段计算。"""
    spec = address.time
    if spec is not None and spec.kind == "frame" and spec.mode in {"single", "range", "list"}:
        values = [value for value in spec.values if value is not None]
        if spec.mode == "range" and spec.values[1] is None:
            return info.frame_count, True
        if values:
            return int(max(values)) + 1, False
    return info.frame_count, True


def plan_capability(config, payload: dict) -> dict:
    """在工作进程内按能力类型估算执行预算；只读取媒体头，不解码像素。"""
    from pixelprobe import core
    from pixelprobe.address import parse_address
    from pixelprobe.capabilities import capability
    from pixelprobe.capabilities.base import Invocation
    from pixelprobe.capabilities.temporal import engine_request

    name = payload["capability"]
    try:
        item = capability(name)
    except KeyError as exc:
        from pixelprobe.capabilities import all_capabilities
        from pixelprobe_mcp.service import McpInputError
        names = "、".join(sorted(entry.name for entry in all_capabilities()))
        raise McpInputError(f"未知能力；可用：{names}") from exc
    if name in LIGHT_CAPABILITIES or not item.needs_media or not payload.get("media_path"):
        return _light_plan()
    path = config.resolve_file_identity(payload["media_path"]).path
    info = core.get_media_info(path)
    address = parse_address(payload.get("at"), payload.get("refs") or {})
    options = item.options.model_validate(payload.get("options") or {})
    call = Invocation(media=path, address=address, options=options)
    request = engine_request(name, call)
    if request is not None:
        return media_plan("generate_representation", info, {"request": request.model_dump(mode="json")})
    if name == "evidence":
        return media_plan("evidence", info, {})
    if name == "generate":
        plans = [
            media_plan("generate_representation", info, {"request": raw})
            for raw in options.requests  # type: ignore[attr-defined]
        ]
        return max(plans, key=lambda plan: plan["memory_bytes"])
    # 组合流程写结果包，图片也要预留临时空间，先于单帧分支判断
    if name in WORKFLOW_CAPABILITIES:
        # 组合流程缺省只检查 0～1799 帧（DEFAULT_WORKFLOW_END_FRAME）
        decoded, _ = _end_frame(address, info)
        if info.media_type != "video":
            decoded = 1
        elif address.time is None or decoded is None:
            decoded = min(info.frame_count or 1800, 1800)
        sample_every = address.step or 1
        infos = [info]
        if payload.get("reference_path"):
            infos.append(core.get_media_info(
                config.resolve_file_identity(payload["reference_path"]).path,
            ))
        pixels = sum(item_info.width * item_info.height for item_info in infos)
        count = decoded // sample_every + 1
        memory = pixels * 96 + count * 2048
        return {
            "estimate_kind": "bounded_streaming_workflow", "full_scan": False,
            "decoded_frames_upper_bound": decoded * len(infos),
            "decode_cost_scope": "one_sequential_pass_per_source_with_lookahead",
            "source_count": len(infos), "stored_frames_estimate": 0,
            "analysis_memory_bytes": memory,
            "memory_bytes": RUNTIME_BYTES + max(16 * MIB, memory),
            "temporary_bytes": max(16 * MIB, pixels * 12 + count * 1024 + CONTROL_BYTES),
            "runtime_allowance_bytes": RUNTIME_BYTES,
        }
    if name in SINGLE_FRAME_CAPABILITIES or info.media_type != "video":
        decoded, full_scan = _end_frame(address, info)
        return media_plan("get_frame", info, {
            "frame": (decoded - 1) if decoded and not full_scan else 0,
            "time_seconds": 0.0 if full_scan and address.time is not None else None,
        })
    if name == "index_build":
        pixels = info.width * info.height
        frames = info.frame_count or 0
        memory = pixels * 24 + MCP_INDEX_THUMB_BUDGET_BYTES + frames * 256
        return {
            "estimate_kind": "index_full_sequential_scan", "full_scan": True,
            "decoded_frames_upper_bound": None, "decoded_frames_per_pass_estimate": frames,
            "stored_frames_estimate": 0, "full_resolution_pixels": pixels,
            "analysis_memory_bytes": memory,
            "memory_bytes": RUNTIME_BYTES + max(16 * MIB, memory),
            "temporary_bytes": 0,
            "runtime_allowance_bytes": RUNTIME_BYTES,
        }
    decoded, full_scan = _end_frame(address, info)
    return media_plan("find_changes", info, {
        "end_frame": None if full_scan else (decoded - 1 if decoded else None),
    })


def plan_operation(config, operation, payload):
    from pixelprobe_mcp.worker import OPERATIONS
    from pixelprobe_mcp import models
    clean = {key: value for key, value in payload.items() if not key.startswith("_")}
    getattr(models, OPERATIONS[operation]).model_validate(clean)
    if operation == "capability":
        return plan_capability(config, clean)
    return {"estimate_kind": "artifact_runtime_guard"}
