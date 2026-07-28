"""时间聚合 Data Tensor 构造。"""

from __future__ import annotations

import numpy as np

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import MemoryArrayHandle, TensorField
from pixelprobe.operators.base import OperatorSpec

REDUCTION_OPERATOR_SPEC = OperatorSpec(
    name="reduce.temporal",
    version="1.0.0",
    category="reduce",
    deterministic="tolerance",
    stateful=True,
    chunkable=True,
    cacheable=True,
    supported_dtypes=("uint8", "float64"),
    config_schema_id="pixelprobe.operator.reduce.temporal.v1",
)


def make_temporal_reduction_tensor(
    statistic: np.ndarray,
    *,
    operation: str,
    source_width: int,
    source_height: int,
    rect: tuple[int, int, int, int] | None,
    frames_analyzed: int,
    presentation_indices: tuple[int, ...] = (),
    timeline_timestamps_seconds: tuple[float, ...] = (),
    parameters: dict[str, object] | None = None,
) -> TensorField:
    if statistic.ndim != 3 or statistic.shape[2] != 3:
        raise ValueError("时间聚合 Data 必须是 [height,width,3]")
    data = statistic.astype(np.float64, copy=False)
    height, width = data.shape[:2]
    offset_x, offset_y = (rect[0], rect[1]) if rect is not None else (0, 0)
    tensor_id = f"tensor_temporal_{operation}_rgb"
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED,
        source=f"{REDUCTION_OPERATOR_SPEC.name}:{REDUCTION_OPERATOR_SPEC.version}",
        assumptions=("float64 accumulation",),
        unit="code_value",
    )
    axes = (
        AxisSpec(
            name="y", kind=AxisKind.Y, length=height, unit="pixel",
            coordinate_mode="regular", start=float(offset_y), step=1.0,
        ),
        AxisSpec(
            name="x", kind=AxisKind.X, length=width, unit="pixel",
            coordinate_mode="regular", start=float(offset_x), step=1.0,
        ),
        AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=3),
    )
    channels = tuple(
        ChannelSpec(
            name=name,
            unit="code_value",
            semantic=f"temporal_{operation}_{name}",
            accuracy=accuracy,
        )
        for name in ("r", "g", "b")
    )
    mappings = tuple(
        AxisMapping(
            mapping_id=f"map_{tensor_id}_{axis}",
            kind="affine",
            input_artifact_id="source_media",
            input_axes=(axis,),
            output_artifact_id=tensor_id,
            output_axes=(axis,),
            parameters={"scale": 1.0, "offset": float(offset)},
            accuracy=AccuracyInfo(
                level=AccuracyLevel.EXACT,
                source="half_open_rect",
                unit="pixel",
            ),
        )
        for axis, offset in (("y", offset_y), ("x", offset_x))
    )
    return TensorField(
        tensor_id=tensor_id,
        data=MemoryArrayHandle(data),
        axes=axes,
        channels=channels,
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels",
            kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"),
            width=source_width,
            height=source_height,
        ),
        axis_mappings=mappings,
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id=f"prov_{tensor_id}"),
        attributes={
            "artifact_role": "data",
            "operation": operation,
            "frames_analyzed": frames_analyzed,
            "presentation_indices": list(presentation_indices),
            "timeline_timestamps_seconds": list(timeline_timestamps_seconds),
            "rect": rect,
            "parameters": parameters or {},
        },
    )
