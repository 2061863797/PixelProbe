"""图片通道语义、调色板/透明度分析与提示标志的结构化输出；CLI 与 MCP 的 info 共用。"""

from __future__ import annotations

from typing import Any

from pixelprobe.core.image_reader import ImageContentAnalysis, NativeImageMetadata


def native_image_metadata_payload(metadata: NativeImageMetadata) -> dict[str, Any]:
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


def image_content_analysis_payload(analysis: ImageContentAnalysis) -> dict[str, Any]:
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
                "mode": analysis.stored_mode, "bands": list(analysis.stored_bands),
                "channel_count": analysis.stored_channel_count,
                "meaning": "存储样本通道；P 模式下是调色板索引，不是灰度或 RGB",
            },
            "analysis_display": {
                "mode": analysis.analysis_display_mode,
                "channel_count": analysis.analysis_display_channel_count,
                "meaning": "确定性颜色算子使用的显示 RGB8",
            },
        },
        "palette": {
            "indexed": analysis.is_indexed,
            "entry_count": analysis.palette_entry_count,
            "used_index_count": analysis.used_palette_index_count,
            "usage_ratio": analysis.palette_usage_ratio,
            "accuracy": "exact", "source": "pillow_palette_and_full_histogram",
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
            "accuracy": "exact", "source": "full_alpha_histogram",
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


def image_analysis_flags(analysis: ImageContentAnalysis) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    if analysis.is_indexed:
        flags.append({
            "code": "INDEXED_COLOR_IMAGE", "severity": "info", "accuracy": "exact",
            "source": "pillow_mode",
            "evidence": {
                "stored_mode": analysis.stored_mode,
                "palette_entries": analysis.palette_entry_count,
                "used_palette_indices": analysis.used_palette_index_count,
            },
            "coverage": "full",
            "message": "图片存储的是调色板索引；单个 P 样本不能直接当作灰度或 RGB。",
        })
    if analysis.is_indexed and analysis.transparency_kind != "none":
        flags.append({
            "code": "PALETTE_TRANSPARENCY", "severity": "info", "accuracy": "exact",
            "source": "full_alpha_histogram",
            "evidence": {
                "kind": analysis.transparency_kind,
                "transparent_ratio": analysis.transparent_ratio,
                "partially_transparent_ratio": analysis.partially_transparent_ratio,
            },
            "coverage": "full",
            "message": "透明度由调色板或颜色键表达。",
        })
    if analysis.transparent_ratio >= 0.95:
        flags.append({
            "code": (
                "FULLY_TRANSPARENT_IMAGE" if analysis.transparent_ratio == 1.0
                else "MOSTLY_TRANSPARENT_IMAGE"
            ),
            "severity": "warning", "accuracy": "exact", "source": "full_alpha_histogram",
            "evidence": {"transparent_ratio": analysis.transparent_ratio},
            "coverage": "full",
            "message": "图片绝大部分像素完全透明，视觉模型可能忽略透明区域中的内容。",
        })
    if analysis.pattern_assessment == "candidate":
        flags.append({
            "code": "REGULAR_PATTERN_CANDIDATE", "severity": "warning",
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
            "message": "检测到规则高频纹理候选，可能干扰视觉识别；该标志不等同于压缩或损坏。",
        })
    return flags
