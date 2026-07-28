"""Farneback 原始流场与派生幅度的 Data Tensor 构造。"""

from __future__ import annotations

import numpy as np
from dataclasses import replace

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import MemoryArrayHandle, TensorField
from pixelprobe.operators.base import HaloSpec, OperatorSpec
from pixelprobe.output.plot import apply_colormap
from pixelprobe.utils.optional_deps import require_cv2

FLOW_OPERATOR_SPEC = OperatorSpec(
    name="transform.optical_flow.farneback",
    version="1.0.0",
    category="transform",
    deterministic="tolerance",
    stateful=True,
    chunkable=False,
    cacheable=True,
    temporal_halo=HaloSpec(before=1),
    supported_dtypes=("float32",),
    config_schema_id="pixelprobe.operator.optical_flow.farneback.v1",
)

_AFFINE_GRID_STEP = 8


def _estimate_global(flow: np.ndarray) -> dict[str, object] | None:
    cv2 = require_cv2()
    height, width = flow.shape[:2]
    ys, xs = np.mgrid[0:height:_AFFINE_GRID_STEP, 0:width:_AFFINE_GRID_STEP]
    source = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(np.float32)
    target = source + flow[ys.ravel(), xs.ravel()].astype(np.float32)
    if len(source) < 3:
        return None
    matrix, _ = cv2.estimateAffinePartial2D(source, target)
    if matrix is None:
        return None
    a, b = float(matrix[0, 0]), float(matrix[1, 0])
    return {
        "dx": round(float(matrix[0, 2]), 4),
        "dy": round(float(matrix[1, 2]), 4),
        "rotation_deg": round(float(np.degrees(np.arctan2(b, a))), 4),
        "scale": round(float(np.hypot(a, b)), 6),
        "matrix": [[round(float(value), 6) for value in row] for row in matrix],
    }


def _subtract_global(flow: np.ndarray, matrix: object) -> np.ndarray:
    height, width = flow.shape[:2]
    transform = np.asarray(matrix, dtype=np.float64)
    ys, xs = np.mgrid[0:height, 0:width]
    predicted_x = transform[0, 0] * xs + transform[0, 1] * ys + transform[0, 2] - xs
    predicted_y = transform[1, 0] * xs + transform[1, 1] * ys + transform[1, 2] - ys
    result = flow.astype(np.float64)
    result[..., 0] -= predicted_x
    result[..., 1] -= predicted_y
    return result


def reconstruct_effective_flow(
    raw_flow: np.ndarray,
    *,
    compensated: bool,
    global_motion: object,
) -> np.ndarray:
    """按 Data 中记录的全局矩阵重建 Preview 使用的 float64 有效流。"""
    if compensated and isinstance(global_motion, dict):
        return _subtract_global(raw_flow, global_motion["matrix"])
    return raw_flow


def build_flow_tensors(
    raw_flow: np.ndarray,
    *,
    frame_a: int,
    frame_b: int,
    frames_analyzed: int,
    accumulated: bool,
    compensate_global: bool,
    mag_threshold: float,
) -> tuple[TensorField, ...]:
    if mag_threshold < 0:
        raise ValueError("mag_threshold 必须 >= 0")
    raw = raw_flow.astype(np.float32, copy=True)
    global_motion = _estimate_global(raw)
    compensated = bool(compensate_global and global_motion is not None)
    effective = (
        _subtract_global(raw, global_motion["matrix"])
        if compensated and global_motion is not None else raw
    )
    magnitude = np.hypot(effective[..., 0], effective[..., 1])
    mask = magnitude > mag_threshold
    bbox = None
    dominant = None
    if mask.any():
        ys, xs = np.nonzero(mask)
        bbox = (
            int(xs.min()), int(ys.min()),
            int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1,
        )
        sum_x = float(effective[..., 0][mask].sum())
        sum_y = float(effective[..., 1][mask].sum())
        if np.hypot(sum_x, sum_y) / int(mask.sum()) > 0.05:
            dominant = round(float(np.degrees(np.arctan2(sum_y, sum_x))), 2)
    metadata: dict[str, object] = {
        "frame_a": frame_a,
        "frame_b": frame_b,
        "frames_analyzed": frames_analyzed,
        "accumulated": accumulated,
        "global_motion": global_motion,
        "compensated": compensated,
        "mag_threshold": mag_threshold,
        "mean_magnitude": round(float(magnitude.mean()), 4),
        "max_magnitude": round(float(magnitude.max()), 4),
        "p95_magnitude": round(float(np.percentile(magnitude, 95)), 4),
        "dominant_angle_deg": dominant,
        "motion_bbox": bbox,
    }
    raw_tensor = make_flow_tensor(
        raw, tensor_id="tensor_flow_raw", frame_a=frame_a,
        frame_b=frame_b, compensated=False,
    )
    flow_tensor = (
        make_flow_tensor(
            effective, tensor_id="tensor_flow_compensated", frame_a=frame_a,
            frame_b=frame_b, compensated=True,
        )
        if compensated else raw_tensor
    )
    magnitude_tensor = make_magnitude_tensor(
        magnitude, source_flow_tensor_id=flow_tensor.tensor_id,
    )
    raw_tensor = replace(raw_tensor, attributes={**raw_tensor.attributes, **metadata})
    if flow_tensor is not raw_tensor:
        flow_tensor = replace(flow_tensor, attributes={**flow_tensor.attributes, **metadata})
    magnitude_tensor = replace(
        magnitude_tensor, attributes={**magnitude_tensor.attributes, **metadata},
    )
    return (
        (raw_tensor, flow_tensor, magnitude_tensor)
        if compensated else (raw_tensor, magnitude_tensor)
    )


def flow_direction_preview(flow: np.ndarray) -> np.ndarray:
    cv2 = require_cv2()
    fx = flow[..., 0].astype(np.float64)
    fy = flow[..., 1].astype(np.float64)
    magnitude = np.hypot(fx, fy)
    maximum = float(magnitude.max())
    hsv = np.zeros((*magnitude.shape, 3), dtype=np.uint8)
    hsv[..., 0] = ((np.degrees(np.arctan2(fy, fx)) % 360.0) / 2.0).astype(np.uint8)
    hsv[..., 1] = 255
    scale = 255.0 / maximum if maximum > 1e-9 else 0.0
    hsv[..., 2] = np.clip(magnitude * scale, 0, 255).astype(np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)


def flow_magnitude_preview(magnitude: np.ndarray) -> np.ndarray:
    maximum = float(magnitude.max())
    scale = 255.0 / maximum if maximum > 1e-9 else 0.0
    return apply_colormap(
        np.clip(magnitude * scale, 0, 255).astype(np.uint8), "fire"
    )


def _space(width: int, height: int) -> CoordinateSpace:
    return CoordinateSpace(
        coordinate_space_id="storage_pixels",
        kind=CoordinateSpaceKind.STORAGE,
        axes=("x", "y"),
        width=width,
        height=height,
    )


def _spatial_mappings(tensor_id: str) -> tuple[AxisMapping, ...]:
    accuracy = AccuracyInfo(
        level=AccuracyLevel.EXACT,
        source="pixel_grid_identity",
        unit="pixel",
    )
    return tuple(
        AxisMapping(
            mapping_id=f"map_{tensor_id}_{axis}",
            kind="affine",
            input_artifact_id="source_media",
            input_axes=(axis,),
            output_artifact_id=tensor_id,
            output_axes=(axis,),
            parameters={"scale": 1.0, "offset": 0.0},
            accuracy=accuracy,
        )
        for axis in ("y", "x")
    )


def make_flow_tensor(
    flow: np.ndarray,
    *,
    tensor_id: str,
    frame_a: int,
    frame_b: int,
    compensated: bool,
) -> TensorField:
    data = flow.astype(np.float32, copy=False)
    if data.ndim != 3 or data.shape[2] != 2:
        raise ValueError("光流 Data 必须是 [height,width,2]")
    height, width = data.shape[:2]
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source=f"{FLOW_OPERATOR_SPEC.name}:{FLOW_OPERATOR_SPEC.version}",
        assumptions=("OpenCV Farneback",),
        unit="pixel",
    )
    return TensorField(
        tensor_id=tensor_id,
        data=MemoryArrayHandle(data),
        axes=(
            AxisSpec(name="y", kind=AxisKind.Y, length=height, unit="pixel", coordinate_mode="regular", start=0.0, step=1.0),
            AxisSpec(name="x", kind=AxisKind.X, length=width, unit="pixel", coordinate_mode="regular", start=0.0, step=1.0),
            AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=2),
        ),
        channels=tuple(
            ChannelSpec(
                name=name,
                unit="pixel",
                semantic=semantic,
                accuracy=accuracy,
            )
            for name, semantic in (
                ("flow_x", "displacement_x"),
                ("flow_y", "displacement_y"),
            )
        ),
        coordinate_space=_space(width, height),
        axis_mappings=_spatial_mappings(tensor_id),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_{tensor_id}"),
        attributes={
            "artifact_role": "data",
            "frame_a": frame_a,
            "frame_b": frame_b,
            "compensated": compensated,
        },
    )


def make_magnitude_tensor(
    magnitude: np.ndarray,
    *,
    source_flow_tensor_id: str,
) -> TensorField:
    data = magnitude.astype(np.float32, copy=False)
    if data.ndim != 2:
        raise ValueError("光流幅度 Data 必须是 [height,width]")
    height, width = data.shape
    tensor_id = f"{source_flow_tensor_id}_magnitude"
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source="hypot(flow_x,flow_y)",
        assumptions=(),
        unit="pixel",
    )
    return TensorField(
        tensor_id=tensor_id,
        data=MemoryArrayHandle(data),
        axes=(
            AxisSpec(name="y", kind=AxisKind.Y, length=height, unit="pixel", coordinate_mode="regular", start=0.0, step=1.0),
            AxisSpec(name="x", kind=AxisKind.X, length=width, unit="pixel", coordinate_mode="regular", start=0.0, step=1.0),
        ),
        channels=(),
        coordinate_space=_space(width, height),
        axis_mappings=_spatial_mappings(tensor_id),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_{tensor_id}"),
        attributes={
            "artifact_role": "data",
            "source_flow_tensor_id": source_flow_tensor_id,
            "semantic": "flow_magnitude",
            "unit": "pixel",
        },
    )
