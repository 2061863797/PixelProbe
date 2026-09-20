"""只读取媒体头的执行预算；展示帧使用顺序解码成本，绝不按平均帧率换算。"""
from __future__ import annotations

MIB = 1024 * 1024
RUNTIME_BYTES = 128 * MIB
CONTROL_BYTES = 4 * MIB


def media_plan(operation, info, payload):
    pixels = info.width * info.height
    decoded, stored, full_scan = 1, 1, False
    # 原生解码、RGB 转换、数组副本及算子浮点工作区。
    memory = pixels * (128 if operation in {"evidence", "inspect_media"} else 32)
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


def plan_operation(config, operation, payload):
    from pixelprobe_mcp.worker import OPERATIONS
    from pixelprobe_mcp import models
    clean = {key: value for key, value in payload.items() if not key.startswith("_")}
    getattr(models, OPERATIONS[operation]).model_validate(clean)
    if "media_path" not in clean:
        return {"estimate_kind": "artifact_runtime_guard"}
    from pixelprobe import core
    path = config.resolve_file_identity(clean["media_path"]).path
    if operation == "workflow":
        from pixelprobe.workflows import WorkflowOptions
        options = WorkflowOptions.model_validate(clean["options"])
        infos = [core.get_media_info(path)]
        if clean.get("reference_path"):
            reference = config.resolve_file_identity(clean["reference_path"]).path
            infos.append(core.get_media_info(reference))
        count = (options.end_frame - options.start_frame) // options.sample_every + 1
        pixels = sum(info.width * info.height for info in infos)
        memory = pixels * 96 + count * 2048
        return {
            "estimate_kind": "bounded_streaming_workflow", "full_scan": False,
            "decoded_frames_upper_bound": sum(options.end_frame + 1 if i.media_type == "video" else 1 for i in infos),
            "decode_cost_scope": "one_sequential_pass_per_source_with_lookahead",
            "source_count": len(infos), "stored_frames_estimate": 0,
            "analysis_memory_bytes": memory,
            "memory_bytes": RUNTIME_BYTES + max(16 * MIB, memory),
            "temporary_bytes": max(16 * MIB, pixels * 12 + count * 1024 + CONTROL_BYTES),
            "runtime_allowance_bytes": RUNTIME_BYTES,
        }
    return media_plan(operation, core.get_media_info(path), clean)
