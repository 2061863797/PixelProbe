"""只读取媒体头的执行预算；展示帧使用顺序解码成本，绝不按平均帧率换算。

估算只用于事先拒绝明显做不完的任务和判断能否加大额度重试；执行期停止线是任务额度，
估低不会导致任务被停。各系数已按 720p～4K 实测校准，不必再为精确度继续调整。
"""
from __future__ import annotations

from pixelprobe.utils.resource_budget import EVIDENCE_BYTES_PER_PIXEL

MIB = 1024 * 1024
RUNTIME_BYTES = 128 * MIB
CONTROL_BYTES = 4 * MIB
VIDEO_DECODE_BYTES_PER_PIXEL = 72
SEQUENTIAL_DECODE_BYTES_PER_PIXEL = 88
# 时间聚合的 float64 工作集倍数，与引擎 _reduce 的预检一致；中位数/百分位需整段物化
_REDUCE_WORKING_MULTIPLIERS = {"min": 2, "max": 2, "mean": 3, "diff": 4, "rms": 4, "std": 5}


def _rect_area(rect, pixels: int) -> int:
    return int(rect[2]) * int(rect[3]) if rect else pixels


def _representation_cost(request, info, pixels: int, stored: int) -> tuple[int, int, int]:
    """返回（结果数值字节、解码之外的分析内存、预览字节基数）。

    已知特征按其真实数据流估算：切片只保留几何采样，时间聚合流式累加，
    光流和二维频谱只处理一对帧/一张图；未知特征（generate 的任意请求）沿用保守的全分辨率模型。
    """
    feature = request.feature.name
    config = request.feature.config
    representation = request.representation
    if representation in {"xt", "yt", "roi_t", "points_t", "path_t"}:
        if representation == "xt":
            samples = info.width
        elif representation == "yt":
            samples = info.height
        elif representation == "roi_t":
            samples = 1
        else:
            samples = int(config.get("sample_count", config.get("point_count", pixels)))
        values = stored * samples * (3 if feature == "rgb" else 32)
        return values, values * (2 if feature == "rgb" else 4), values
    if representation == "frames" and request.reduction is not None and feature == "rgb":
        area = _rect_area(request.reduction.config.get("rect"), pixels)
        operation = request.reduction.name
        multiplier = _REDUCE_WORKING_MULTIPLIERS.get(operation, stored + 1)
        # float64 工作集 + 共享帧存储写入与回读的映射页
        working = area * 3 * 8 * multiplier + stored * pixels * 3 * 2
        return area * 3 * 8, working, area * 3
    if representation == "feature_t" and feature == "temporal_fft":
        values = stored * _rect_area(config.get("rect"), 1) * 8
        return values, values * 16, 0
    if representation == "feature_t" and feature == "farneback":
        # 两帧灰度、二通道 float32 光流场、幅值与预览；累积模式额外保留累加场
        return pixels * 8, pixels * (80 if config.get("accumulate") else 64), 0
    if representation == "feature_t" and feature == "spatial_fft":
        area = _rect_area(config.get("rect"), pixels)
        return area * 8, area * 48, 0
    values = stored * pixels * (3 if feature == "rgb" else 32)
    extra = values * (4 if request.reduction or feature != "rgb" else 2)
    if feature in {"flow", "farneback"}:
        extra += pixels * 128
    if "fft" in feature or "frequency" in feature or feature == "stft":
        extra += values * 16
    return values, extra, values


def media_plan(operation, info, payload):
    pixels = info.width * info.height
    decoded, stored, full_scan = 1, 1, False
    # 原生解码、RGB 转换、数组副本及算子浮点工作区。
    # evidence 用实测峰值校准的系数；inspect_media 尚未实测，保持原保守值；
    # 视频解码含解码器参考帧与格式转换缓冲；无缓存时实测 1080p/4K 扣除运行时余量后约 56～60 字节/像素，取 72 留余量
    default_per_pixel = VIDEO_DECODE_BYTES_PER_PIXEL if info.media_type == "video" else 32
    if operation == "find_changes" and info.media_type == "video":
        # 顺序扫描同时保留上一帧和多项逐帧统计的工作数组；实测 2K/4K 约 70 字节/像素
        default_per_pixel = SEQUENTIAL_DECODE_BYTES_PER_PIXEL
    per_pixel = {"evidence": EVIDENCE_BYTES_PER_PIXEL, "inspect_media": 128}.get(
        operation, default_per_pixel,
    )
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
        values, extra, preview = _representation_cost(request, info, pixels, stored)
        # 帧 mmap、缓存复制、持久化副本和 PNG/元数据均计入；运行时按真实编码字节复核。
        frames = stored * pixels * 3 + (pixels * 8 if info.media_type == "image" else 0)
        disk = frames + values * 4 + 4 * MIB
        if request.reduction is not None or request.representation == "feature_t":
            # 整段帧作为中间结果供下游计算时会落到临时文件（引擎自动外溢），再占一份帧空间
            disk += stored * pixels * 3
        memory += extra
        if info.media_type == "video" and request.reduction is None:
            # 共享帧存储写入时驻留的映射页；实测 4K 切片比单帧多约 8 帧 RGB（时间聚合已单独计入回读）
            memory += min(stored, 8) * pixels * 3
        if request.output.include_preview:
            memory += preview * 8
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
        # 实测 quality_overview 在 720p～4K 上分析内存约 110～115 字节/像素（扣除运行时余量）
        memory = pixels * 132 + count * 2048
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
        # 建索引同样顺序解码全分辨率帧，按顺序扫描系数估算
        memory = pixels * SEQUENTIAL_DECODE_BYTES_PER_PIXEL + MCP_INDEX_THUMB_BUDGET_BYTES + frames * 256
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
