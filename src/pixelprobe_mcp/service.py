"""复用 PixelProbe Python API 的 MCP 业务薄层。"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image as PILImage
from pydantic import ValidationError

import pixelprobe
from pixelprobe import core
from pixelprobe.artifacts import BundleReader
from pixelprobe.core.image_reader import (
    ImageContentAnalysis,
    ImageReader,
    NativeImageMetadata,
)
from pixelprobe.core.media_reader import detect_media_type
from pixelprobe.domain.media import MediaSource
from pixelprobe.engine.request import OutputRequest, RepresentationRequest
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.operators.base import ResourcePolicy
from pixelprobe.version import __version__
from pixelprobe_mcp.config import FileIdentity, ServerConfig
from pixelprobe_mcp.models import (
    ArtifactReadInput,
    BundleListInput,
    ChangesInput,
    FrameInput,
    GenerateInput,
    InspectInput,
    NextAction,
    Pagination,
    PixelInput,
    RegionInput,
    ToolEnvelope,
)

CHARACTER_LIMIT = 25_000
MCP_MAX_STANDARD_SOURCE_FRAMES = 1_800
MCP_MAX_CHANGE_SOURCE_FRAMES = 3_600
MCP_MAX_GRID_POINTS = 65_536
MCP_MAX_GENERATE_SOURCE_FRAMES = 1_800
MCP_MAX_FRAME_WORKING_BYTES = 256 * 1024 * 1024
MCP_GENERATE_RESOURCES = ResourcePolicy(
    max_memory_bytes=256 * 1024 * 1024,
    max_temporary_bytes=2 * 1024 * 1024 * 1024,
    timeout_seconds=120.0,
    preferred_chunk_bytes=32 * 1024 * 1024,
    allow_partial=False,
)


class McpResourceLimitError(ValueError):
    """MCP 外层资源边界拒绝执行，以免分页被误解为计算限制。"""

AGENT_GUIDANCE = """你正在使用 PixelProbe 分析本地图片或视频。

工作原则：
1. 语义理解、人物/物体/文字/场景和构图判断，以 Agent 自身视觉能力为主。
2. PixelProbe 是确定性辅助：用于确认帧号、时间、坐标、像素颜色、区域统计、变化候选和正式数值表示。
3. 不要仅凭变化曲线、光流或频谱推断具体事件；先定位候选，再调用 pixelprobe_get_frame 查看前、中、后原始画面。
4. 先调用 pixelprobe_inspect_media。图片通常随后查看原图并按需读像素/区域；视频先依据代表帧、事件和异常选择要看的帧。
5. pixelprobe_get_frame 不会缩放。若载荷过大，应裁剪、分区查看或使用精确像素/区域工具，不得把缩略图当作原始像素。
6. Preview 只用于视觉展示；需要数值结论时读取 DataArtifact，并保留帧号、时间、坐标与来源说明。
7. 技术异常和内容语义必须分开陈述；PixelProbe 的候选标志不是造假、目标存在或事件发生的证明。
8. 图片的存储通道、分析 RGB 通道和视觉 PNG 通道含义不同；P 模式样本是调色板索引，必须结合 palette、alpha 和 flags 解读。
"""


def _page(total: int, offset: int, count: int) -> Pagination:
    next_offset = offset + count
    has_more = next_offset < total
    return Pagination(
        total=total,
        count=count,
        offset=offset,
        has_more=has_more,
        next_offset=next_offset if has_more else None,
    )


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _ensure_response_size(response: ToolEnvelope) -> ToolEnvelope:
    size = _json_size(response.model_dump(mode="json"))
    if size > CHARACTER_LIMIT:
        raise ValueError(
            f"响应约 {size} 字符，超过 {CHARACTER_LIMIT}；请减小 limit、缩小选择范围或分页读取"
        )
    return response


def _rect(value: object) -> tuple[int, int, int, int] | None:
    return value.as_tuple() if value is not None else None  # type: ignore[union-attr]


def _encode_png(array: np.ndarray) -> bytes:
    output = io.BytesIO()
    PILImage.fromarray(np.ascontiguousarray(array)).save(output, format="PNG")
    return output.getvalue()


def _native_image_metadata_payload(metadata: NativeImageMetadata) -> dict[str, Any]:
    """把图片原生样本的说明转成 MCP 可序列化数据。"""
    return {
        "sample_semantics": metadata.sample_semantics,
        "mode": metadata.mode,
        "dtype": metadata.dtype,
        "shape": list(metadata.shape),
        "bands": list(metadata.bands),
        "channel_count": metadata.channel_count,
        "bits_per_sample": metadata.bits_per_sample,
        "has_alpha": metadata.has_alpha,
        "alpha_representation": metadata.alpha_representation,
    }


def _image_content_analysis_payload(analysis: ImageContentAnalysis) -> dict[str, Any]:
    """把完整结构统计整理成便于 Agent 判断的分层字段。"""
    pattern_evidence = None
    if analysis.pattern_sample_rect is not None:
        pattern_evidence = {
            "sample_rect": list(analysis.pattern_sample_rect),
            "horizontal_period_pixels": analysis.pattern_horizontal_period_pixels,
            "horizontal_correlation": analysis.pattern_horizontal_correlation,
            "vertical_period_pixels": analysis.pattern_vertical_period_pixels,
            "vertical_correlation": analysis.pattern_vertical_correlation,
            "high_frequency_stddev": analysis.pattern_high_frequency_stddev,
        }
    return {
        "total_pixels": analysis.total_pixels,
        "channel_semantics": {
            "stored": {
                "mode": analysis.stored_mode,
                "bands": list(analysis.stored_bands),
                "channel_count": analysis.stored_channel_count,
                "meaning": "存储样本通道；P 模式下是调色板索引，不是灰度或 RGB",
            },
            "analysis_display": {
                "mode": analysis.analysis_display_mode,
                "channel_count": analysis.analysis_display_channel_count,
                "meaning": "现有确定性颜色算子使用的显示 RGB8",
            },
            "visual_output": {
                "mode": analysis.visual_output_mode,
                "channel_count": analysis.visual_output_channel_count,
                "meaning": "pixelprobe_get_frame 返回给 Agent 视觉的 PNG 通道",
            },
        },
        "palette": {
            "indexed": analysis.is_indexed,
            "entry_count": analysis.palette_entry_count,
            "used_index_count": analysis.used_palette_index_count,
            "usage_ratio": analysis.palette_usage_ratio,
            "accuracy": "exact",
            "source": "pillow_palette_and_full_histogram",
        },
        "alpha": {
            "representation": analysis.transparency_kind,
            "level_count": analysis.alpha_level_count,
            "transparent_pixels": analysis.transparent_pixel_count,
            "partially_transparent_pixels": analysis.partially_transparent_pixel_count,
            "opaque_pixels": analysis.opaque_pixel_count,
            "transparent_ratio": analysis.transparent_ratio,
            "partially_transparent_ratio": analysis.partially_transparent_ratio,
            "opaque_ratio": analysis.opaque_ratio,
            "accuracy": "exact",
            "source": "full_alpha_histogram",
        },
        "regular_pattern": {
            "assessment": analysis.pattern_assessment,
            "accuracy": analysis.pattern_accuracy,
            "coverage": analysis.pattern_coverage,
            "evidence": pattern_evidence,
            "interpretation": (
                "仅表示二维高频残差存在规则周期相关性；可能来自刻意网屏、"
                "调色板抖动或画面纹理，不能单独证明压缩、损坏或伪造"
            ),
        },
    }


def _image_analysis_flags(analysis: ImageContentAnalysis) -> list[dict[str, Any]]:
    """从确定性统计生成可追溯提醒，不推断未知成因。"""
    flags: list[dict[str, Any]] = []
    if analysis.is_indexed:
        flags.append({
            "code": "INDEXED_COLOR_IMAGE",
            "severity": "info",
            "accuracy": "exact",
            "source": "pillow_mode",
            "evidence": {
                "stored_mode": analysis.stored_mode,
                "palette_entries": analysis.palette_entry_count,
                "used_palette_indices": analysis.used_palette_index_count,
            },
            "coverage": "full",
            "message": "图片存储的是调色板索引；单个 P 样本不能直接当作灰度或 RGB。",
            "recommended_tool": "pixelprobe_get_frame",
        })
    if analysis.is_indexed and analysis.transparency_kind != "none":
        flags.append({
            "code": "PALETTE_TRANSPARENCY",
            "severity": "info",
            "accuracy": "exact",
            "source": "full_alpha_histogram",
            "evidence": {
                "kind": analysis.transparency_kind,
                "transparent_ratio": analysis.transparent_ratio,
                "partially_transparent_ratio": analysis.partially_transparent_ratio,
            },
            "coverage": "full",
            "message": "透明度由调色板或颜色键表达，视觉输出会展开为 RGBA。",
            "recommended_tool": "pixelprobe_get_frame",
        })
    if analysis.transparent_ratio >= 0.95:
        flags.append({
            "code": (
                "FULLY_TRANSPARENT_IMAGE"
                if analysis.transparent_ratio == 1.0
                else "MOSTLY_TRANSPARENT_IMAGE"
            ),
            "severity": "warning",
            "accuracy": "exact",
            "source": "full_alpha_histogram",
            "evidence": {"transparent_ratio": analysis.transparent_ratio},
            "coverage": "full",
            "message": "图片绝大部分像素完全透明，视觉模型可能忽略透明区域中的内容。",
            "recommended_tool": "pixelprobe_get_frame",
        })
    if analysis.pattern_assessment == "candidate":
        flags.append({
            "code": "REGULAR_PATTERN_CANDIDATE",
            "severity": "warning",
            "accuracy": analysis.pattern_accuracy,
            "source": "pixelprobe_high_frequency_periodicity",
            "evidence": {
                "sample_rect": list(analysis.pattern_sample_rect or ()),
                "horizontal_period_pixels": analysis.pattern_horizontal_period_pixels,
                "horizontal_correlation": analysis.pattern_horizontal_correlation,
                "vertical_period_pixels": analysis.pattern_vertical_period_pixels,
                "vertical_correlation": analysis.pattern_vertical_correlation,
            },
            "coverage": analysis.pattern_coverage,
            "message": (
                "检测到规则高频纹理候选，可能干扰视觉识别；该标志不等同于压缩或损坏。"
            ),
            "recommended_tool": "pixelprobe_get_frame",
        })
    return flags


def _native_sample_value(array: np.ndarray, x: int, y: int) -> object:
    """返回一个图片存储样本；标量和多通道样本均可 JSON 编码。"""
    value = np.asarray(array[y, x])
    if value.ndim == 0:
        return value.item()
    return value.tolist()


def _read_image_samples(
    path: Path,
) -> tuple[np.ndarray, np.ndarray, NativeImageMetadata, MediaInfo]:
    """读取图片的显示 RGB8 与原生样本，二者绝不混为同一精度语义。"""
    with ImageReader() as reader:
        reader.open(path)
        native = reader.get_native_frame()
        metadata = reader.get_native_metadata()
        display = reader.get_engine_frame()
        info = reader.get_info()
    return display, native, metadata, info


def _read_image_metadata(path: Path) -> tuple[NativeImageMetadata, str, tuple[str, ...]]:
    """只取得图片显示转换语义，不把视觉 PNG 误标为原生像素。"""
    with ImageReader() as reader:
        reader.open(path)
        return (
            reader.get_native_metadata(),
            reader.engine_sample_semantics(),
            reader.engine_conversion_flags(),
        )


def _read_alpha_visual_frame(
    path: Path, crop: tuple[int, int, int, int] | None,
) -> np.ndarray:
    """为视觉 ImageContent 保留透明通道；统计/计算仍使用明确标记的 RGB8。"""
    with PILImage.open(path) as image:
        visual = image.convert("RGBA")
        try:
            array = np.array(visual, dtype=np.uint8, copy=True, order="C")
        finally:
            visual.close()
    if crop is not None:
        x, y, width, height = crop
        return array[y : y + height, x : x + width, :].copy()
    return array


class PixelProbeService:
    """只负责编排现有核心，不复制媒体分析算法。"""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config

    def _resolve_media(self, value: str) -> tuple[Path, FileIdentity]:
        identity = self.config.resolve_file_identity(value)
        return identity.path, identity

    def _verify_media_unchanged(self, identity: FileIdentity) -> None:
        self.config.verify_file_identity(identity)

    @staticmethod
    def _require_bounded_video_frame_count(
        info: MediaInfo, *, operation: str,
    ) -> int:
        if info.media_type != "video":
            raise McpResourceLimitError(f"{operation} 仅支持视频输入")
        if info.frame_count is None:
            raise McpResourceLimitError(
                f"{operation} 需要可用的总帧数；请先使用 quick 检查，"
                "或改用带帧数元数据的短视频"
            )
        return info.frame_count

    @classmethod
    def _ensure_standard_budget(cls, info: MediaInfo) -> None:
        frame_count = cls._require_bounded_video_frame_count(info, operation="standard 检查")
        if frame_count > MCP_MAX_STANDARD_SOURCE_FRAMES:
            raise McpResourceLimitError(
                "standard 检查最多处理 "
                f"{MCP_MAX_STANDARD_SOURCE_FRAMES} 个源帧，当前媒体有 {frame_count} 帧；"
                "请先使用 quick，再针对短片段调用变化检测"
            )

    @classmethod
    def _ensure_changes_budget(cls, info: MediaInfo, params: ChangesInput) -> None:
        if params.grid is not None:
            step = params.grid_step or 1
            columns = (params.grid.width + step - 1) // step
            rows = (params.grid.height + step - 1) // step
            point_count = columns * rows
            if point_count > MCP_MAX_GRID_POINTS:
                raise McpResourceLimitError(
                    "网格变化检测最多使用 "
                    f"{MCP_MAX_GRID_POINTS} 个采样点，当前网格会生成 {point_count} 个；"
                    "请缩小 grid 或增大 grid_step"
                )
        frame_count = cls._require_bounded_video_frame_count(info, operation="变化检测")
        if params.start_seconds is not None or params.end_seconds is not None:
            if frame_count > MCP_MAX_CHANGE_SOURCE_FRAMES:
                raise McpResourceLimitError(
                    "长视频按时间变化检测无法在 MCP 中安全估算解码量；"
                    "请改用不超过 "
                    f"{MCP_MAX_CHANGE_SOURCE_FRAMES} 帧的 start_frame/end_frame 范围"
                )
            return
        start = params.start_frame if params.start_frame is not None else 0
        end = params.end_frame if params.end_frame is not None else frame_count - 1
        if end < start:
            return
        source_frame_count = end - start + 1
        if source_frame_count > MCP_MAX_CHANGE_SOURCE_FRAMES:
            raise McpResourceLimitError(
                "变化检测最多处理 "
                f"{MCP_MAX_CHANGE_SOURCE_FRAMES} 个源帧，当前范围包含 {source_frame_count} 帧；"
                "分页只限制返回结果，不会限制解码，请缩小 start_frame/end_frame"
            )

    @classmethod
    def _ensure_generation_budget(
        cls, info: MediaInfo, request: RepresentationRequest,
    ) -> None:
        if info.media_type != "video":
            return
        frame_count = cls._require_bounded_video_frame_count(info, operation="表示生成")
        selection = request.selection
        if selection.mode == "indices":
            source_frame_count = len(selection.requested_indices)
        elif selection.mode == "frame_interval":
            assert selection.requested_start_frame is not None
            assert selection.requested_end_frame_exclusive is not None
            source_frame_count = (
                selection.requested_end_frame_exclusive - selection.requested_start_frame
            )
        elif selection.mode == "time_interval":
            source_frame_count = frame_count
        else:
            source_frame_count = frame_count
        if source_frame_count > MCP_MAX_GENERATE_SOURCE_FRAMES:
            raise McpResourceLimitError(
                "表示生成最多处理 "
                f"{MCP_MAX_GENERATE_SOURCE_FRAMES} 个源帧，当前选择包含至少 "
                f"{source_frame_count} 帧；请缩小选择范围。sample_every 不会避免底层解码"
            )

    def _ensure_frame_budget(
        self,
        info: MediaInfo,
        crop: object | None = None,
        *,
        include_image_payload: bool,
    ) -> None:
        source_rgb_bytes = info.width * info.height * 3
        # 仅限制发给视觉模型的“完整画面”请求。精确像素/区域工具不受像素上限限制；
        # 带 crop 的画面请求按返回区域估算，绝不静默缩放。
        working_bytes = source_rgb_bytes * 4
        if crop is None and working_bytes > MCP_MAX_FRAME_WORKING_BYTES:
            raise McpResourceLimitError(
                "原始全帧解码工作集约 "
                f"{working_bytes} 字节，超过 MCP 限制 {MCP_MAX_FRAME_WORKING_BYTES} 字节；"
                "请提供 crop 分区查看；精确像素或区域统计仍可单独调用"
            )
        if not include_image_payload:
            return
        rect = _rect(crop)
        width = rect[2] if rect is not None else info.width
        height = rect[3] if rect is not None else info.height
        # PNG 对不可压缩 RGB 的编码量接近 4 字节/像素；预检避免先构造巨大 BytesIO。
        estimated_png_bytes = width * height * 4 + 65_536
        estimated_base64_bytes = ((estimated_png_bytes + 2) // 3) * 4
        if estimated_base64_bytes > self.config.max_image_bytes:
            crop_hint = (
                "请提供更小的 crop"
                if rect is not None
                else "请提供 crop 分区查看"
            )
            raise McpResourceLimitError(
                "预计 MCP 图片载荷约 "
                f"{estimated_base64_bytes} 字节，超过限制 {self.config.max_image_bytes} 字节；"
                f"{crop_hint}。服务不会缩放原始画面"
            )

    def capabilities(self) -> ToolEnvelope:
        data = {
            "server": "pixelprobe_mcp",
            "pixelprobe_version": __version__,
            "transport": "stdio",
            "principle": "Agent 原生视觉为主，PixelProbe 确定性数据为辅助",
            "allowed_roots": [str(root) for root in self.config.allowed_roots],
            "artifact_root": str(self.config.artifact_root),
            "limits": {
                "max_points_per_call": 256,
                "max_artifact_values_per_call": 4096,
                "max_image_payload_bytes": self.config.max_image_bytes,
                "max_text_characters": CHARACTER_LIMIT,
                "max_standard_source_frames": MCP_MAX_STANDARD_SOURCE_FRAMES,
                "max_change_source_frames": MCP_MAX_CHANGE_SOURCE_FRAMES,
                "max_grid_points": MCP_MAX_GRID_POINTS,
                "max_generate_source_frames": MCP_MAX_GENERATE_SOURCE_FRAMES,
                "max_frame_working_bytes": MCP_MAX_FRAME_WORKING_BYTES,
                "generate_resources": MCP_GENERATE_RESOURCES.model_dump(mode="json"),
            },
            "timeout_semantics": (
                "MCP 在超时后会停止等待结果；已进入底层同步解码的任务无法被安全强杀，"
                "会继续占用服务器操作槽位直到自行结束"
            ),
            "tools": [
                "pixelprobe_get_capabilities",
                "pixelprobe_inspect_media", "pixelprobe_get_frame",
                "pixelprobe_read_pixels", "pixelprobe_analyze_region",
                "pixelprobe_find_changes", "pixelprobe_generate_representation",
                "pixelprobe_list_artifacts", "pixelprobe_read_artifact",
            ],
            "guarantees": [
                "帧号按显示顺序从 0 开始", "坐标基于原始存储像素",
                "精确工具不静默缩放", "Preview 与 DataArtifact 分离",
                "图片存储通道、分析通道与视觉输出通道分别描述",
                "图片异常候选包含精度、来源、证据与覆盖范围",
            ],
        }
        return ToolEnvelope(data=data)

    def inspect_media(self, params: InspectInput) -> ToolEnvelope:
        path, identity = self._resolve_media(params.media_path)
        is_image = detect_media_type(path) == "image"
        image_metadata: NativeImageMetadata | None = None
        image_analysis: ImageContentAnalysis | None = None
        image_engine_semantics: str | None = None
        image_conversion_flags: tuple[str, ...] = ()
        if is_image:
            # ImageReader.open 已完成解码；在同一实例取得基础信息与原生样本描述，
            # 避免 quick 检查为同一图片重复解码。
            with ImageReader() as reader:
                reader.open(path)
                info = reader.get_info()
                image_metadata = reader.get_native_metadata()
                image_analysis = reader.get_content_analysis()
                image_engine_semantics = reader.engine_sample_semantics()
                image_conversion_flags = reader.engine_conversion_flags()
        else:
            info = core.get_media_info(path)
        accuracy = {
            "file_size_bytes": "exact", "width": "decoded", "height": "decoded",
        }
        if info.media_type == "video":
            accuracy["frame_count"] = (
                "estimated" if info.frame_count_estimated else "decoded"
            )
        data: dict[str, Any] = {
            "info": info.model_dump(mode="json"),
            "accuracy": accuracy,
        }
        if image_metadata is not None and image_engine_semantics is not None:
            data["image_samples"] = {
                "native": _native_image_metadata_payload(image_metadata),
                "engine_sample_semantics": image_engine_semantics,
                "engine_conversion_flags": list(image_conversion_flags),
            }
            assert image_analysis is not None
            data["image_analysis"] = _image_content_analysis_payload(image_analysis)
            data["flags"] = _image_analysis_flags(image_analysis)
        else:
            data["sample_semantics"] = "decoded_sample"
        warnings: list[str] = []
        if image_analysis is not None and image_analysis.is_indexed:
            warnings.append(
                "索引色图片：info.channels 表示存储索引通道，不等于展开后的显示通道"
            )
        if image_analysis is not None and image_analysis.pattern_assessment == "candidate":
            warnings.append(
                "检测到规则高频纹理候选；它可能影响视觉识别，但不能单独证明压缩、损坏或伪造"
            )
        if image_analysis is not None and image_analysis.transparent_ratio >= 0.95:
            warnings.append("图片至少 95% 的像素完全透明，理解内容时请保留 Alpha 通道")
        actions = [
            NextAction(
                tool="pixelprobe_get_frame",
                reason="用 Agent 自身视觉理解原始画面；视频默认先看第 0 帧",
                arguments={"media_path": str(path), "frame": 0} if info.media_type == "video"
                else {"media_path": str(path)},
            )
        ]
        pagination = None
        if params.detail == "standard" and info.media_type == "video":
            self._ensure_standard_budget(info)
            scan = core.scan_media(path)
            combined = [
                {"category": "event", **event.to_dict()} for event in scan.events
            ] + [
                {"category": "anomaly", **anomaly} for anomaly in scan.anomalies
            ]
            page_items = combined[params.offset : params.offset + params.limit]
            pagination = _page(len(combined), params.offset, len(page_items))
            data.update({
                "scan": {
                    "effective_sample_every": scan.effective_sample_every,
                    "frames_analyzed": scan.frames_analyzed,
                    "representative_frames": [
                        {"frame": frame, "time_seconds": timestamp}
                        for frame, timestamp in zip(scan.sheet.frames, scan.sheet.times)
                    ],
                    "event_threshold_used": scan.event_threshold,
                    "event_count": len(scan.events),
                    "anomaly_count": len(scan.anomalies),
                    "anomalies_truncated_at_source": scan.anomalies_truncated,
                    "findings": page_items,
                }
            })
            candidate_frames = sorted({
                int(item["frame"] if "frame" in item else item["peak_frame"])
                for item in page_items
                if "frame" in item or "peak_frame" in item
            })[:5]
            if candidate_frames:
                actions.append(NextAction(
                    tool="pixelprobe_get_frame",
                    reason="逐个查看候选帧，并结合相邻帧后再解释变化",
                    arguments={"media_path": str(path), "frame": candidate_frames[0]},
                ))
            if scan.effective_sample_every > 1:
                warnings.append(
                    f"standard 扫描每 {scan.effective_sample_every} 帧采样一次；精确结论需缩小范围复查"
                )
        self._verify_media_unchanged(identity)
        return _ensure_response_size(ToolEnvelope(
            data=data, warnings=warnings, next_actions=actions, pagination=pagination,
        ))

    def get_frame(self, params: FrameInput) -> tuple[ToolEnvelope, bytes]:
        path, identity = self._resolve_media(params.media_path)
        source_info = core.get_media_info(path)
        self._ensure_frame_budget(
            source_info, params.crop, include_image_payload=True,
        )
        array, index, timestamp, info = core.get_frame(
            path,
            frame=params.frame,
            time=params.time_seconds,
            crop=_rect(params.crop),
        )
        native_image = None
        visual_sample_semantics = "decoded_sample"
        conversion_flags: list[str] = []
        if info.media_type == "image":
            metadata, engine_semantics, flags = _read_image_metadata(path)
            native_image = _native_image_metadata_payload(metadata)
            # core.get_frame 的图片分支返回既有 RGB8 计算帧；它是视觉展示值，
            # 非 RGB8 原生图会经过显式转换，不能声称为 stored/decoded 原样本。
            visual_sample_semantics = engine_semantics
            conversion_flags = list(flags)
            if metadata.has_alpha:
                array = _read_alpha_visual_frame(path, _rect(params.crop))
                if metadata.mode == "RGBA" and metadata.dtype == "uint8":
                    visual_sample_semantics = "decoded_rgba8"
                    conversion_flags = ["VISUAL_ALPHA_PRESERVED"]
                else:
                    visual_sample_semantics = "display_rgba8"
                    conversion_flags = [
                        "DISPLAY_RGBA8_CONVERSION",
                        "NATIVE_IMAGE_PRESERVED",
                        "VISUAL_ALPHA_PRESERVED",
                    ]
        png = _encode_png(array)
        encoded_base64_bytes = ((len(png) + 2) // 3) * 4
        if encoded_base64_bytes > self.config.max_image_bytes:
            raise ValueError(
                f"编码后的 MCP PNG 载荷为 {encoded_base64_bytes} 字节，超过限制 "
                f"{self.config.max_image_bytes}；请提供 crop 分区查看。服务不会静默缩放"
            )
        self._verify_media_unchanged(identity)
        response = ToolEnvelope(
            data={
                "path": str(path), "media_type": info.media_type,
                "frame": index, "time_seconds": timestamp,
                "source_width": info.width, "source_height": info.height,
                "returned_width": int(array.shape[1]),
                "returned_height": int(array.shape[0]),
                "crop": params.crop.model_dump() if params.crop else None,
                "image_mime_type": "image/png", "image_bytes": len(png),
                "image_base64_bytes": encoded_base64_bytes,
                "resized": False, "sample_semantics": visual_sample_semantics,
                "native_image": native_image,
                "conversion_flags": conversion_flags,
            },
            next_actions=[NextAction(
                tool="pixelprobe_read_pixels",
                reason="对视觉判断中的关键位置读取确定性 RGB/HSV/Lab/亮度",
                arguments={"media_path": str(path), "frame": index} if index is not None
                else {"media_path": str(path)},
            )],
        )
        return _ensure_response_size(response), png

    def read_pixels(self, params: PixelInput) -> ToolEnvelope:
        path, identity = self._resolve_media(params.media_path)
        native: np.ndarray | None = None
        native_metadata: NativeImageMetadata | None = None
        if detect_media_type(path) == "image":
            if params.frame is not None or params.time_seconds is not None:
                # 与 core.load_frame 的兼容错误语义保持一致，避免图片把 frame/time
                # 静默忽略成第 0 帧。
                array, index, timestamp, info = core.load_frame(
                    path, frame=params.frame, time=params.time_seconds,
                )
            else:
                array, native, native_metadata, info = _read_image_samples(path)
                index = timestamp = None
        else:
            array, index, timestamp, info = core.load_frame(
                path, frame=params.frame, time=params.time_seconds,
            )
        samples = core.inspect_pixels(
            array, [(point.x, point.y) for point in params.points],
            frame=index, time_seconds=timestamp,
        )
        sample_semantics = "decoded_sample"
        stored_sample_metadata = None
        pixels = [sample.model_dump(mode="json") for sample in samples]
        if native is not None and native_metadata is not None:
            sample_semantics = "decoded_rgb8" if native_metadata.mode == "RGB" and native_metadata.dtype == "uint8" else "display_rgb8"
            stored_sample_metadata = _native_image_metadata_payload(native_metadata)
            for item, point in zip(pixels, params.points):
                item["stored_sample"] = _native_sample_value(native, point.x, point.y)
        self._verify_media_unchanged(identity)
        return _ensure_response_size(ToolEnvelope(data={
            "path": str(path), "media_type": info.media_type,
            "width": info.width, "height": info.height,
            "frame": index, "time_seconds": timestamp,
            "coordinate_space": "storage_pixels",
            "sample_semantics": sample_semantics,
            "stored_sample_metadata": stored_sample_metadata,
            "pixels": pixels,
        }))

    def analyze_region(self, params: RegionInput) -> ToolEnvelope:
        path, identity = self._resolve_media(params.media_path)
        native_metadata: NativeImageMetadata | None = None
        if detect_media_type(path) == "image":
            if params.frame is not None or params.time_seconds is not None:
                array, index, timestamp, info = core.load_frame(
                    path, frame=params.frame, time=params.time_seconds,
                )
            else:
                array, _native, native_metadata, info = _read_image_samples(path)
                index = timestamp = None
        else:
            array, index, timestamp, info = core.load_frame(
                path, frame=params.frame, time=params.time_seconds,
            )
        statistics = core.analyze_region(array, params.rect.as_tuple())
        sample_semantics = "decoded_sample"
        stored_sample_metadata = None
        warnings: list[str] = []
        if native_metadata is not None:
            sample_semantics = "decoded_rgb8" if native_metadata.mode == "RGB" and native_metadata.dtype == "uint8" else "display_rgb8"
            stored_sample_metadata = _native_image_metadata_payload(native_metadata)
            if sample_semantics == "display_rgb8":
                warnings.append(
                    "区域统计基于显示 RGB8 转换；原生位深、Alpha、调色板或颜色模式未被当作 RGB 数值统计"
                )
        self._verify_media_unchanged(identity)
        return _ensure_response_size(ToolEnvelope(data={
            "path": str(path), "media_type": info.media_type,
            "frame": index, "time_seconds": timestamp,
            "coordinate_space": "storage_pixels",
            "sample_semantics": sample_semantics,
            "stored_sample_metadata": stored_sample_metadata,
            "statistics": statistics.model_dump(mode="json"),
        }, warnings=warnings))

    def find_changes(self, params: ChangesInput) -> ToolEnvelope:
        path, identity = self._resolve_media(params.media_path)
        self._ensure_changes_budget(core.get_media_info(path), params)
        result = core.detect_changes(
            path,
            point=(params.point.x, params.point.y) if params.point else None,
            rect=_rect(params.rect), grid=_rect(params.grid), step=params.grid_step,
            start_frame=params.start_frame, end_frame=params.end_frame,
            start=params.start_seconds, end=params.end_seconds,
            sample_every=params.sample_every,
        )
        records = result.records
        if params.sort == "score":
            records = sorted(records, key=lambda item: (-item.score, item.frame))
        page_records = records[params.offset : params.offset + params.limit]
        pagination = _page(len(records), params.offset, len(page_records))
        top_frame = page_records[0].frame if page_records else None
        actions = []
        if top_frame is not None:
            actions.append(NextAction(
                tool="pixelprobe_get_frame",
                reason="变化分数没有语义；查看候选帧及其前后帧后再判断",
                arguments={"media_path": str(path), "frame": top_frame},
            ))
        self._verify_media_unchanged(identity)
        return _ensure_response_size(ToolEnvelope(
            data={
                "path": str(path), "mode": result.mode,
                "width": result.width, "height": result.height,
                "frames_analyzed": result.frames_analyzed,
                "frame_range": {
                    "start": result.frame_range.start,
                    "end_inclusive": result.frame_range.end,
                    "sample_every": result.frame_range.sample_every,
                },
                "sort": params.sort,
                "records": [record.to_dict() for record in page_records],
            },
            warnings=["变化分数只表示像素差异，不能单独证明对象移动或事件发生"],
            next_actions=actions,
            pagination=pagination,
        ))

    def generate_representation(self, params: GenerateInput) -> ToolEnvelope:
        path, identity = self._resolve_media(params.media_path)
        request = RepresentationRequest.model_validate(params.request)
        source_info = core.get_media_info(path)
        self._ensure_generation_budget(source_info, request)
        request = request.model_copy(update={
            "source": MediaSource(
                source_id=request.source.source_id,
                kind="file",
                uri=str(path),
                declared_media_type=request.source.declared_media_type,
            ),
            "output": OutputRequest(
                format="bundle",
                include_preview=request.output.include_preview,
                preview_config=request.output.preview_config,
                metadata_policy=request.output.metadata_policy,
            ),
            "resources": MCP_GENERATE_RESOURCES,
        })
        name = params.output_name or f"result-{uuid.uuid4().hex}.bundle"
        if not name.endswith(".bundle"):
            name += ".bundle"
        output = self.config.prepare_artifact_target(name)
        result = pixelprobe.generate(request, output_path=output)
        if result.bundle is None:
            raise RuntimeError("生成完成但没有返回 Bundle")
        self._verify_media_unchanged(identity)
        data_records = [
            record.artifact_id for record in result.bundle.manifest.artifacts
            if record.kind == "data"
        ]
        return _ensure_response_size(ToolEnvelope(
            data={
                "bundle_path": str(result.bundle.root),
                "bundle_id": result.bundle.manifest.bundle_id,
                "schema_version": result.bundle.manifest.schema_version,
                "plan_id": result.plan.plan_id,
                "decode_passes": result.decode_passes,
                "artifact_count": len(result.bundle.manifest.artifacts),
                "data_artifact_ids": data_records,
            },
            next_actions=[NextAction(
                tool="pixelprobe_read_artifact",
                reason="读取正式数值时按轴选择分页，Preview 不能替代 DataArtifact",
                arguments={
                    "bundle_path": str(result.bundle.root),
                    "artifact_id": data_records[0] if data_records else "",
                },
            )],
        ))

    def list_artifacts(self, params: BundleListInput) -> ToolEnvelope:
        root = self.config.resolve_directory(params.bundle_path)
        bundle = BundleReader().open(root, verify=params.verify)
        records = [
            record for record in bundle.manifest.artifacts
            if params.kind is None or record.kind == params.kind
        ]
        page_records = records[params.offset : params.offset + params.limit]
        return _ensure_response_size(ToolEnvelope(
            data={
                "bundle_path": str(root), "bundle_id": bundle.manifest.bundle_id,
                "schema_version": bundle.manifest.schema_version,
                "verify": params.verify,
                "notices": list(bundle.notices),
                "artifacts": [record.model_dump(mode="json") for record in page_records],
            },
            pagination=_page(len(records), params.offset, len(page_records)),
        ))

    def read_artifact(self, params: ArtifactReadInput) -> ToolEnvelope:
        root = self.config.resolve_directory(params.bundle_path)
        bundle = BundleReader().open(root, verify=params.verify)
        tensor = bundle.open_tensor(params.artifact_id)
        try:
            data: dict[str, Any] = {
                "bundle_path": str(root), "artifact_id": params.artifact_id,
                "verify": params.verify,
                "tensor_id": tensor.tensor_id, "shape": list(tensor.data.shape),
                "dtype": tensor.data.dtype,
                "axes": [axis.model_dump(mode="json") for axis in tensor.axes],
                "channels": [channel.model_dump(mode="json") for channel in tensor.channels],
                "coordinate_space": (
                    tensor.coordinate_space.model_dump(mode="json")
                    if tensor.coordinate_space else None
                ),
                "accuracy": tensor.accuracy.model_dump(mode="json"),
                "selection": None, "selected_shape": None, "values": None,
            }
            actions: list[NextAction] = []
            if params.selection is None:
                actions.append(NextAction(
                    tool="pixelprobe_read_artifact",
                    reason="按 axes 顺序提供 selection 后读取有限数量正式数值",
                    arguments={
                        "bundle_path": str(root), "artifact_id": params.artifact_id,
                        "selection": [
                            {"start": 0, "stop": min(axis.length, 1)}
                            for axis in tensor.axes
                        ],
                    },
                ))
            else:
                if len(params.selection) != len(tensor.data.shape):
                    raise ValueError(
                        f"selection 需要 {len(tensor.data.shape)} 个轴，实际 {len(params.selection)}"
                    )
                selection: list[slice | int] = []
                count = 1
                for axis, (item, length) in enumerate(zip(params.selection, tensor.data.shape)):
                    if item.index is not None:
                        selection.append(item.index)
                        continue
                    start = 0 if item.start is None else item.start
                    stop = length if item.stop is None else item.stop
                    if start > stop or stop > length:
                        raise ValueError(f"第 {axis} 轴切片必须满足 0 <= start <= stop <= {length}")
                    selection.append(slice(start, stop, item.step))
                    count *= len(range(start, stop, item.step))
                if count > params.max_values:
                    raise ValueError(
                        f"选择包含 {count} 个标量，超过 max_values={params.max_values}；请缩小切片"
                    )
                array = tensor.data.read(tuple(selection))
                data["selection"] = [item.model_dump(mode="json") for item in params.selection]
                data["selected_shape"] = list(array.shape)
                data["value_count"] = int(array.size)
                if np.iscomplexobj(array):
                    data["values"] = {
                        "real": array.real.tolist(), "imag": array.imag.tolist(),
                    }
                else:
                    data["values"] = array.tolist()
            warnings = []
            if params.verify == "metadata":
                warnings.append(
                    "当前为 metadata 验证：已检查结构和文件大小，未重新计算全部 SHA-256；"
                    "需要完整内容完整性校验时请以 verify='full' 重试"
                )
            return _ensure_response_size(ToolEnvelope(
                data=data, warnings=warnings, next_actions=actions,
            ))
        finally:
            close = getattr(tensor.data, "close", None)
            if callable(close):
                close()


def validation_message(exc: ValidationError) -> str:
    """把 Pydantic 错误压缩为 Agent 可直接修正的消息。"""
    items = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(item) for item in error["loc"]) or "请求"
        items.append(f"{location}: {error['msg']}")
    return "；".join(items)
