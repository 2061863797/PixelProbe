"""Preview Tensor 构造；预览永不覆盖数值 Data Tensor。"""

from __future__ import annotations

import numpy as np

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import MemoryArrayHandle, TensorField
from pixelprobe.operators.base import OperatorSpec

PREVIEW_OPERATOR_SPEC = OperatorSpec(
    name="preview.image",
    version="1.0.0",
    category="preview",
    deterministic="bit_exact",
    stateful=False,
    chunkable=True,
    cacheable=True,
    supported_dtypes=("uint8",),
    config_schema_id="pixelprobe.operator.preview.image.v1",
)


def temporal_reduction_preview(
    statistic: np.ndarray,
    *,
    p_low: float = 1.0,
    p_high: float = 99.0,
    destripe: bool = False,
    smooth: int = 0,
) -> tuple[np.ndarray, dict[str, object]]:
    """把时间聚合 Data 转成旧 CLI 兼容的显示图，不修改原数值。"""
    if statistic.ndim != 3 or statistic.shape[2] != 3:
        raise ValueError("时间聚合 Preview 输入必须是 [height,width,3]")
    if not 0.0 <= p_low < p_high <= 100.0:
        raise ValueError("Preview 百分位必须满足 0 <= p_low < p_high <= 100")
    if not 0 <= smooth <= 64:
        raise ValueError("Preview smooth 必须在 0～64 内")
    display = statistic.astype(np.float64, copy=True)
    domains: list[str] = []
    if destripe:
        display = (
            display
            - display.mean(axis=0, keepdims=True)
            - display.mean(axis=1, keepdims=True)
            + display.mean(axis=(0, 1), keepdims=True)
        )
        domains.append("detrended_residual")
    if smooth >= 2:
        pad_lo = smooth // 2
        pad_hi = smooth - 1 - pad_lo
        padded = np.pad(
            display,
            ((pad_lo, pad_hi), (pad_lo, pad_hi), (0, 0)),
            mode="edge",
        )
        integral = np.pad(
            padded, ((1, 0), (1, 0), (0, 0))
        ).cumsum(axis=0).cumsum(axis=1)
        display = (
            integral[smooth:, smooth:]
            - integral[:-smooth, smooth:]
            - integral[smooth:, :-smooth]
            + integral[:-smooth, :-smooth]
        ) / (smooth * smooth)
        domains.append("smoothed")
    low = float(np.percentile(display, p_low))
    high = float(np.percentile(display, p_high))
    if high - low < 1e-12:
        image = np.full(display.shape, 128, dtype=np.uint8)
    else:
        scaled = np.clip((display - low) / (high - low), 0.0, 1.0)
        image = (scaled * 255.0 + 0.5).astype(np.uint8)
    return image, {
        "normalization": "percentile",
        "p_low": p_low,
        "p_high": p_high,
        "stretch_low_value": round(low, 4),
        "stretch_high_value": round(high, 4),
        "stretch_domain": "+".join(domains) if domains else "raw",
        "destripe": destripe,
        "smooth": smooth,
    }


def make_preview_tensor(
    image: np.ndarray,
    *,
    tensor_id: str,
    source_tensor_id: str,
    source_width: int,
    source_height: int,
    attributes: dict[str, object] | None = None,
) -> TensorField:
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Preview 必须是 [height,width,3] uint8 RGB")
    height, width = image.shape[:2]
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source=f"{PREVIEW_OPERATOR_SPEC.name}:{PREVIEW_OPERATOR_SPEC.version}",
        assumptions=("display-only representation",),
        unit="display_code_value",
    )
    axes = (
        AxisSpec(
            name="y", kind=AxisKind.Y, length=height, unit="pixel",
            coordinate_mode="regular", start=0.0,
            step=source_height / height,
        ),
        AxisSpec(
            name="x", kind=AxisKind.X, length=width, unit="pixel",
            coordinate_mode="regular", start=0.0,
            step=source_width / width,
        ),
        AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=3),
    )
    channels = tuple(
        ChannelSpec(
            name=name,
            unit="display_code_value",
            semantic=f"preview_srgb_{semantic}",
            value_range=(0, 255),
            accuracy=accuracy,
        )
        for name, semantic in (("r", "red"), ("g", "green"), ("b", "blue"))
    )
    mappings = tuple(
        AxisMapping(
            mapping_id=f"map_{tensor_id}_{axis}",
            kind="affine",
            input_artifact_id=source_tensor_id,
            input_axes=(axis,),
            output_artifact_id=tensor_id,
            output_axes=(axis,),
            parameters={"scale": source / output, "offset": 0.0},
            accuracy=accuracy,
        )
        for axis, source, output in (
            ("y", source_height, height),
            ("x", source_width, width),
        )
    )
    return TensorField(
        tensor_id=tensor_id,
        data=MemoryArrayHandle(image),
        axes=axes,
        channels=channels,
        coordinate_space=CoordinateSpace(
            coordinate_space_id=f"preview_space_{tensor_id}",
            kind=CoordinateSpaceKind.DISPLAY,
            axes=("x", "y"),
            width=width,
            height=height,
            parent_space_id="storage_pixels",
        ),
        axis_mappings=mappings,
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_{tensor_id}"),
        attributes={
            "artifact_role": "preview",
            "source_tensor_id": source_tensor_id,
            **(attributes or {}),
        },
    )
