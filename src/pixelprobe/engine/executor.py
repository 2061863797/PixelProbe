"""统一请求的本地 CPU Executor；同一媒体只建立一个 FrameStore。"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
from importlib.metadata import PackageNotFoundError, version as package_version
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from pixelprobe.artifacts import Bundle, BundleWriter, sha256_file
from pixelprobe.artifacts.array_io import NpyArrayHandle, save_array_handle_npy
from pixelprobe.artifacts.models import SourceRecord
from pixelprobe.core.frame_selector import FrameRange
from pixelprobe.core.media_reader import detect_actual_format
from pixelprobe.core.optical_flow import _farneback, _gray
from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pydantic import TypeAdapter

from pixelprobe.domain.geometry import (
    Geometry,
    PathGeometry,
    PointGeometry,
    RectGeometry,
)
from pixelprobe.domain.media import MediaIdentity
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.errors import (
    MaterializationLimitExceededError,
    MediaChangedDuringAnalysisError,
)
from pixelprobe.domain.tensor import ArrayHandle, MemoryArrayHandle, TensorField
from pixelprobe.engine.chunks import NpyArtifactSink
from pixelprobe.engine.execution import (
    CheckpointRecord,
    LocalExecutionContext,
    encoded_state,
)
from pixelprobe.engine.cache import CacheKeyInput
from pixelprobe.engine.errors import (
    CheckpointIncompatibleError,
    ResourcePlanUnsatisfiableError,
)
from pixelprobe.engine.graph import GraphNode, canonical_json
from pixelprobe.engine.frame_store import FramePacketMetadata, SharedFrameStore
from pixelprobe.engine.operator_registry import OperatorRegistry
from pixelprobe.engine.planner import ExecutionPlan, PlannedNode
from pixelprobe.engine.request import RepresentationRequest
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.operators.common import memory_array_ref
from pixelprobe.operators.optical_flow import (
    build_flow_tensors,
    flow_direction_preview,
    flow_magnitude_preview,
    reconstruct_effective_flow,
)
from pixelprobe.operators.base import RuntimeInvocation, TensorChunk
from pixelprobe.operators.preview import (
    make_preview_tensor,
    temporal_reduction_preview,
)
from pixelprobe.operators.frequency import (
    spatial_fft_from_frame,
    spatial_fft_preview,
    stft_from_series,
    temporal_fft_from_series,
    temporal_fft_preview,
)
from pixelprobe.operators.reduction import make_temporal_reduction_tensor
from pixelprobe.operators.sampling import (
    SamplingPlan,
    _build_tensor,
    _sample_frame,
    resample_polyline,
)
from pixelprobe.operators.transforms import (
    hsv_to_rgb,
    lab_to_rgb,
    rgb_to_grayscale,
    rgb_to_hsv,
    rgb_to_lab,
)
from pixelprobe.utils.optional_deps import require_cv2


@dataclass(slots=True, frozen=True)
class GenerationResult:
    plan: ExecutionPlan
    request_tensors: tuple[tuple[TensorField, ...], ...]
    bundle: Bundle | None
    events: tuple[dict[str, object], ...]
    decode_passes: int
    cache_hits: int = 0
    cache_writes: int = 0

    @property
    def tensors(self) -> tuple[TensorField, ...]:
        return tuple(tensor for group in self.request_tensors for tensor in group)


def _rgb_channels(accuracy: AccuracyInfo) -> tuple[ChannelSpec, ...]:
    return tuple(
        ChannelSpec(
            name=name, unit="code_value", semantic=f"decoded_srgb_{semantic}",
            value_range=(0, 255), accuracy=accuracy,
        )
        for name, semantic in (("r", "red"), ("g", "green"), ("b", "blue"))
    )


def _serialize_frame_metadata(
    metadata: tuple[FramePacketMetadata, ...] | None,
) -> list[dict[str, object]]:
    """把运行时帧包元数据转换为可持久化 JSON，保留整数 PTS 与有理 time_base。"""
    if metadata is None:
        return []
    return [
        {
            "presentation_index": item.presentation_index,
            "decode_index": item.decode_index,
            "pts": item.pts,
            "dts": item.dts,
            "time_base": (
                {"numerator": item.time_base.numerator, "denominator": item.time_base.denominator}
                if item.time_base is not None else None
            ),
            "source_timestamp_seconds": item.source_timestamp_seconds,
            "timeline_time_seconds": item.timeline_time_seconds,
            "duration_pts": item.duration_pts,
            "duration_seconds": item.duration_seconds,
            "key_frame": item.key_frame,
            "stored_pixel_format": item.stored_pixel_format,
            "decoded_pixel_format": item.decoded_pixel_format,
            "color_metadata": item.color_metadata,
            "sample_semantics": item.sample_semantics,
            "flags": list(item.flags),
        }
        for item in metadata
    ]


def _optional_integer_index(
    values: tuple[int | None, ...], prefix: str,
) -> tuple[object, np.ndarray, object, np.ndarray]:
    present = np.asarray([value is not None for value in values], dtype=bool)
    numeric = np.asarray([0 if value is None else value for value in values], dtype=np.int64)
    return (
        memory_array_ref(numeric, prefix), numeric,
        memory_array_ref(present, f"{prefix}_present"), present,
    )


def _native_image_tensor(
    store: SharedFrameStore, source_id: str,
) -> TensorField | None:
    """把图片的 Pillow 原生样本作为独立 DataArtifact，而非混入 RGB8 计算帧。"""
    data = store.native_image
    metadata = store.native_image_metadata
    if data is None or metadata is None:
        return None
    if len(data.shape) not in {2, 3}:
        raise InvalidRangeError("原生图片样本必须是二维或三维数组")
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DECODED,
        source="pillow_native_image_sample",
        unit="code_value",
    )
    axes: list[AxisSpec] = [
        AxisSpec(
            name="y", kind=AxisKind.Y, length=data.shape[0], unit="pixel",
            coordinate_mode="regular", start=0.0, step=1.0,
        ),
        AxisSpec(
            name="x", kind=AxisKind.X, length=data.shape[1], unit="pixel",
            coordinate_mode="regular", start=0.0, step=1.0,
        ),
    ]
    channels: tuple[ChannelSpec, ...] = ()
    if len(data.shape) == 3:
        if len(metadata.bands) != data.shape[2]:
            raise InvalidRangeError("原生图片通道元数据与数组 shape 不一致")
        axes.append(AxisSpec(
            name="channel", kind=AxisKind.CHANNEL, length=data.shape[2],
        ))
        channels = tuple(
            ChannelSpec(
                name=band.lower(), semantic=f"stored_{band.lower()}",
                unit="code_value", accuracy=accuracy,
            )
            for band in metadata.bands
        )
    return TensorField(
        tensor_id="tensor_image_native",
        data=data,
        axes=tuple(axes),
        channels=channels,
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels",
            kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"), width=store.width, height=store.height,
        ),
        axis_mappings=(),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id="prov_pillow_native_image"),
        attributes={
            "artifact_role": "data",
            "dag_node_id": f"native_image:{source_id}",
            "sample_semantics": metadata.sample_semantics,
            "source_id": source_id,
            "native_image": {
                "mode": metadata.mode,
                "source_format": metadata.source_format,
                "dtype": metadata.dtype,
                "shape": list(metadata.shape),
                "bands": list(metadata.bands),
                "bits_per_sample": metadata.bits_per_sample,
                "has_alpha": metadata.has_alpha,
                "alpha_representation": metadata.alpha_representation,
                "sample_semantics": metadata.sample_semantics,
            },
        },
    )


def _frames_tensor(
    array: np.ndarray | ArrayHandle,
    indices: tuple[int, ...],
    times: tuple[float, ...],
    nominal_fps: float | None = None,
    frame_metadata: tuple[FramePacketMetadata, ...] | None = None,
) -> TensorField:
    if frame_metadata is not None and (
        len(frame_metadata) != len(indices)
        or tuple(item.presentation_index for item in frame_metadata) != indices
    ):
        raise InvalidRangeError("FrameStore 元数据与所选展示帧不一致")
    data = MemoryArrayHandle(array) if isinstance(array, np.ndarray) else array
    height, width = data.shape[1:3]
    tensor_id = "tensor_frames_rgb"
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DECODED, source="shared_frame_store", unit="code_value"
    )
    time_values = np.asarray(times, dtype=np.float64)
    time_ref = memory_array_ref(time_values, f"index_{tensor_id}_time")
    index_values: dict[str, object] = {time_ref.artifact_id: time_values}
    mapping_parameters: dict[str, object] = {
        "coordinates_ref": time_ref.artifact_id,
        "presentation_indices": list(indices),
    }
    serialized_metadata = _serialize_frame_metadata(frame_metadata)
    if frame_metadata is not None:
        presentation_ref = memory_array_ref(
            np.asarray(indices, dtype=np.int64), f"index_{tensor_id}_presentation_index",
        )
        index_values[presentation_ref.artifact_id] = np.asarray(indices, dtype=np.int64)
        mapping_parameters["presentation_indices_ref"] = presentation_ref.artifact_id
        for field_name, values in (
            ("pts", tuple(item.pts for item in frame_metadata)),
            ("dts", tuple(item.dts for item in frame_metadata)),
            ("duration_pts", tuple(item.duration_pts for item in frame_metadata)),
        ):
            numeric_ref, numeric, present_ref, present = _optional_integer_index(
                values, f"index_{tensor_id}_{field_name}",
            )
            index_values[numeric_ref.artifact_id] = numeric
            index_values[present_ref.artifact_id] = present
            mapping_parameters[f"{field_name}_ref"] = numeric_ref.artifact_id
            mapping_parameters[f"{field_name}_present_ref"] = present_ref.artifact_id
        time_base_numerator = np.asarray([
            0 if item.time_base is None else item.time_base.numerator
            for item in frame_metadata
        ], dtype=np.int64)
        time_base_denominator = np.asarray([
            0 if item.time_base is None else item.time_base.denominator
            for item in frame_metadata
        ], dtype=np.int64)
        time_base_present = np.asarray([
            item.time_base is not None for item in frame_metadata
        ], dtype=bool)
        for name, values in (
            ("time_base_numerator", time_base_numerator),
            ("time_base_denominator", time_base_denominator),
            ("time_base_present", time_base_present),
        ):
            reference = memory_array_ref(values, f"index_{tensor_id}_{name}")
            index_values[reference.artifact_id] = values
            mapping_parameters[f"{name}_ref"] = reference.artifact_id
    return TensorField(
        tensor_id=tensor_id,
        data=data,
        axes=(
            AxisSpec(
                name="time", kind=AxisKind.TIME, length=len(indices), unit="second",
                coordinate_mode="irregular", coordinates_ref=time_ref,
                mapping_id=f"map_{tensor_id}_time",
            ),
            AxisSpec(name="y", kind=AxisKind.Y, length=height, unit="pixel", coordinate_mode="regular", start=0.0, step=1.0),
            AxisSpec(name="x", kind=AxisKind.X, length=width, unit="pixel", coordinate_mode="regular", start=0.0, step=1.0),
            AxisSpec(name="channel", kind=AxisKind.CHANNEL, length=3),
        ),
        channels=_rgb_channels(accuracy),
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels", kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"), width=width, height=height,
        ),
        axis_mappings=(AxisMapping(
            mapping_id=f"map_{tensor_id}_time", kind="lookup",
            input_artifact_id="source_media", input_axes=("time",),
            output_artifact_id=tensor_id, output_axes=("time",),
            parameters=mapping_parameters,
            accuracy=AccuracyInfo(
                level=AccuracyLevel.DERIVED,
                source="decoded_frame_pts_and_time_base", unit="second",
            ),
        ),),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(provenance_id="prov_shared_decode_rgb24"),
        attributes={
            "artifact_role": "data",
            "presentation_indices": list(indices),
            "timeline_timestamps_seconds": list(times),
            "frame_metadata": serialized_metadata,
            "nominal_fps": nominal_fps,
            "_index_values": index_values,
        },
    )


def _normalize_preview(data: np.ndarray) -> np.ndarray:
    values = np.asarray(data)
    if values.dtype == np.uint8:
        normalized = values
    else:
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            normalized = np.zeros(values.shape, dtype=np.uint8)
        else:
            low, high = np.percentile(finite, (1.0, 99.0))
            if high <= low:
                normalized = np.zeros(values.shape, dtype=np.uint8)
            else:
                normalized = np.clip((values - low) * 255.0 / (high - low), 0, 255).astype(np.uint8)
    if normalized.ndim == 4:
        normalized = normalized[0]
    if normalized.ndim == 2:
        if normalized.shape[1] == 3:
            normalized = normalized[:, None, :]
        else:
            normalized = np.repeat(normalized[..., None], 3, axis=2)
    if normalized.ndim == 3 and normalized.shape[-1] == 2:
        magnitude = np.hypot(normalized[..., 0], normalized[..., 1]).astype(np.uint8)
        normalized = np.repeat(magnitude[..., None], 3, axis=2)
    if normalized.ndim != 3 or normalized.shape[2] != 3:
        raise InvalidRangeError("该 Data Tensor 暂无默认 RGB Preview")
    return np.ascontiguousarray(normalized)


def _preview(
    tensor: TensorField,
    max_memory_bytes: int,
    config: dict[str, object],
) -> TensorField:
    if tensor.axes and tensor.axes[0].kind == AxisKind.TIME:
        values = tensor.data.read((
            0, *(slice(None) for _ in tensor.data.shape[1:]),
        ))
    else:
        values = tensor.data.materialize(max_bytes=max_memory_bytes)
    preview_attributes: dict[str, object]
    if config.get("mode", "auto") == "temporal_reduce":
        image, preview_attributes = temporal_reduction_preview(
            values,
            p_low=float(config.get("p_low", 1.0)),
            p_high=float(config.get("p_high", 99.0)),
            destripe=bool(config.get("destripe", False)),
            smooth=int(config.get("smooth", 0)),
        )
    elif tensor.attributes.get("color_model") == "grayscale":
        gray = np.clip(np.rint(values), 0, 255).astype(np.uint8)
        image = np.repeat(gray[..., None], 3, axis=2)
        preview_attributes = {"normalization": "none", "color_model": "grayscale"}
    elif tensor.attributes.get("color_model") == "hsv":
        image = hsv_to_rgb(values)
        preview_attributes = {"normalization": "none", "color_model": "hsv"}
    elif tensor.attributes.get("color_model") == "lab":
        image = lab_to_rgb(values)
        preview_attributes = {"normalization": "none", "color_model": "lab"}
    else:
        image = _normalize_preview(values)
        preview_attributes = {"normalization": "percentile_1_99"}
    height, width = image.shape[:2]
    return make_preview_tensor(
        image,
        tensor_id=f"preview_{tensor.tensor_id}",
        source_tensor_id=tensor.tensor_id,
        source_width=(tensor.coordinate_space.width if tensor.coordinate_space else width),
        source_height=(tensor.coordinate_space.height if tensor.coordinate_space else height),
        attributes=preview_attributes,
    )


def _rows_to_handle(
    node_id: str,
    indices: tuple[int, ...],
    row_reader: Callable[[int], np.ndarray],
    context: LocalExecutionContext,
    *,
    file_backed: bool,
    partial_path: Path | None = None,
    completed_positions: set[int] | None = None,
    completed_hashes: dict[int, str] | None = None,
    on_chunk_completed: Callable[[int, str], None] | None = None,
) -> ArrayHandle:
    """逐帧读取结果；文件输出不堆叠，内存输出严格服从预算。"""
    if not indices:
        raise InvalidRangeError("帧选择不能为空")
    first = np.ascontiguousarray(row_reader(indices[0]))
    shape = (len(indices), *first.shape)
    total_bytes = int(np.prod(shape, dtype=np.int64)) * first.dtype.itemsize
    if file_backed:
        temporary_limit = context.resources.max_temporary_bytes
        if temporary_limit is not None and total_bytes > temporary_limit:
            raise ResourcePlanUnsatisfiableError(
                f"节点 {node_id} 需要 {total_bytes} 字节临时空间，"
                f"超过限制 {temporary_limit} 字节"
            )
        completed_positions = completed_positions or set()
        completed_hashes = completed_hashes or {}
        if partial_path is not None:
            partial_path.parent.mkdir(parents=True, exist_ok=True)
            if completed_positions and not partial_path.is_file():
                raise CheckpointIncompatibleError(
                    f"checkpoint 的部分结果已缺失：{node_id}"
                )
            array = np.lib.format.open_memmap(
                partial_path,
                mode="r+" if partial_path.exists() else "w+",
                dtype=first.dtype,
                shape=shape,
            )
            try:
                if array.shape != shape or array.dtype != first.dtype:
                    raise CheckpointIncompatibleError(
                        f"checkpoint 的部分数组不匹配：{node_id}"
                    )
                for position in completed_positions:
                    if position < 0 or position >= len(indices):
                        raise CheckpointIncompatibleError(
                            f"checkpoint chunk 越界：{node_id}/{position}"
                        )
                    expected_hash = completed_hashes.get(position)
                    actual_hash = hashlib.sha256(
                        np.ascontiguousarray(array[position]).tobytes(order="C")
                    ).hexdigest()
                    if expected_hash != actual_hash:
                        raise CheckpointIncompatibleError(
                            f"checkpoint chunk 校验失败：{node_id}/{position}"
                        )
                for position, index in enumerate(indices):
                    context.ensure_active()
                    if position in completed_positions:
                        continue
                    row = first if position == 0 else np.ascontiguousarray(row_reader(index))
                    if row.shape != first.shape or row.dtype != first.dtype:
                        raise InvalidRangeError("采样结果的 shape 或 dtype 在帧间发生变化")
                    array[position] = row
                    array.flush()
                    with partial_path.open("r+b") as output:
                        os.fsync(output.fileno())
                    if on_chunk_completed is not None:
                        on_chunk_completed(
                            position,
                            hashlib.sha256(row.tobytes(order="C")).hexdigest(),
                        )
                mmap = getattr(array, "_mmap", None)
                if mmap is not None:
                    mmap.close()
                return NpyArrayHandle(partial_path)
            except Exception:
                mmap = getattr(array, "_mmap", None)
                if mmap is not None:
                    mmap.close()
                raise
        sink = NpyArtifactSink(
            context.temporary_root / f"{node_id}.npy",
            shape,
            str(first.dtype),
            expected_chunks=len(indices),
        )
        try:
            for position, index in enumerate(indices):
                context.ensure_active()
                row = first if position == 0 else np.ascontiguousarray(row_reader(index))
                if row.shape != first.shape or row.dtype != first.dtype:
                    raise InvalidRangeError("采样结果的 shape 或 dtype 在帧间发生变化")
                selection = (
                    slice(position, position + 1),
                    *(slice(0, length) for length in first.shape),
                )
                sink.write(TensorChunk(
                    tensor_id=node_id,
                    data=row[None, ...],
                    read_selection=selection,
                    core_selection=selection,
                    axis_mappings=(),
                    validity=None,
                    chunk_index=(position,),
                ))
            return sink.finalize()
        except Exception:
            sink.abort()
            raise

    # MemoryArrayHandle 会建立自己的只读副本，因此预算同时计入工作数组和结果副本。
    peak_bytes = total_bytes * 2 + first.nbytes
    if peak_bytes > context.resources.max_memory_bytes:
        raise MaterializationLimitExceededError(
            f"节点 {node_id} 精确内存结果预计峰值 {peak_bytes} 字节，"
            f"超过限制 {context.resources.max_memory_bytes} 字节；"
            "请使用 Bundle/Zarr 输出或提高 max_memory_bytes"
        )
    array = np.empty(shape, dtype=first.dtype)
    array[0] = first
    for position, index in enumerate(indices[1:], start=1):
        context.ensure_active()
        row = np.ascontiguousarray(row_reader(index))
        if row.shape != first.shape or row.dtype != first.dtype:
            raise InvalidRangeError("采样结果的 shape 或 dtype 在帧间发生变化")
        array[position] = row
    return MemoryArrayHandle(array)


def _reduce(
    tensor: TensorField, operation: str,
    max_memory_bytes: int,
    config: dict[str, object],
) -> TensorField:
    indices = tuple(int(item) for item in tensor.attributes["presentation_indices"])
    times = tuple(float(item) for item in tensor.attributes["timeline_timestamps_seconds"])
    if tensor.coordinate_space is None:
        raise InvalidRangeError("帧 Tensor 缺少存储坐标空间")
    source_width = tensor.coordinate_space.width
    source_height = tensor.coordinate_space.height
    if source_width is None or source_height is None:
        raise InvalidRangeError("帧 Tensor 缺少源尺寸")
    rect_value = config.get("rect")
    rect = tuple(int(value) for value in rect_value) if rect_value is not None else None
    if rect is not None:
        from pixelprobe.utils.coordinates import validate_rect
        validate_rect(*rect, source_width, source_height)

    def read(position: int) -> np.ndarray:
        value = tensor.data.read((position, slice(None), slice(None), slice(None)))
        if rect is not None:
            x, y, width, height = rect
            value = np.ascontiguousarray(value[y:y + height, x:x + width, :])
        return value

    first = read(0).astype(np.float64)
    working_multipliers = {
        "min": 2,
        "max": 2,
        "mean": 3,
        "diff": 4,
        "rms": 4,
        "std": 5,
    }
    if operation in working_multipliers:
        required = working_multipliers[operation] * first.nbytes
        if required > max_memory_bytes:
            raise MaterializationLimitExceededError(
                f"时间聚合 {operation} 的精确工作集至少需要 {required} 字节，"
                f"超过限制 {max_memory_bytes} 字节"
            )
    if operation == "diff":
        if len(indices) < 2:
            raise InvalidRangeError("op=diff 至少需要两帧")
        previous = read(0)
        total = np.zeros(first.shape, dtype=np.float64)
        for position in range(1, len(indices)):
            current = read(position)
            total += np.abs(current.astype(np.float64) - previous.astype(np.float64))
            previous = current
        statistic = total / (len(indices) - 1)
    elif operation in {"median", "percentile"}:
        needed = (len(indices) + 1) * first.nbytes
        if needed > max_memory_bytes:
            raise MaterializationLimitExceededError(
                f"精确 {operation} 至少需要 {needed} 字节，"
                f"超过限制 {max_memory_bytes} 字节"
            )
        values = np.empty((len(indices), *first.shape), dtype=np.float64)
        values[0] = first
        for position in range(1, len(indices)):
            values[position] = read(position)
        statistic = (
            np.median(values, axis=0)
            if operation == "median"
            else np.percentile(values, float(config["percentile"]), axis=0)
        )
    elif operation == "min":
        statistic = first
        for position in range(1, len(indices)):
            np.minimum(statistic, read(position), out=statistic)
    elif operation == "max":
        statistic = first
        for position in range(1, len(indices)):
            np.maximum(statistic, read(position), out=statistic)
    else:
        total = first
        square_total = first * first if operation in {"std", "rms"} else None
        for position in range(1, len(indices)):
            value = read(position).astype(np.float64)
            total += value
            if square_total is not None:
                square_total += value * value
        mean = total / len(indices)
        if operation == "std":
            assert square_total is not None
            statistic = np.sqrt(np.maximum(
                square_total / len(indices) - mean * mean, 0.0,
            ))
        elif operation == "rms":
            assert square_total is not None
            statistic = np.sqrt(square_total / len(indices))
        elif operation == "mean":
            statistic = mean
        else:
            raise InvalidRangeError(f"不支持的时间聚合：{operation}")
    return make_temporal_reduction_tensor(
        statistic,
        operation=operation,
        source_width=source_width,
        source_height=source_height,
        rect=rect,
        frames_analyzed=len(indices),
        presentation_indices=indices,
        timeline_timestamps_seconds=times,
        parameters={
            "percentile": config.get("percentile"),
        } if operation == "percentile" else {},
    )


class LocalExecutor:
    def __init__(self, registry: OperatorRegistry | None = None) -> None:
        self.registry = registry or OperatorRegistry()
        self.registry.register_runtime(node_type="sample", handler=self._runtime_sample)
        self.registry.register_runtime(
            name="feature.flow", handler=self._runtime_transform,
        )
        self.registry.register_runtime(
            name="feature.farneback", handler=self._runtime_transform,
        )
        self.registry.register_runtime(
            node_type="transform", handler=self._runtime_transform,
        )
        self.registry.register_runtime(node_type="frequency", handler=self._runtime_frequency)
        self.registry.register_runtime(node_type="reduce", handler=self._runtime_reduce)
        self.registry.register_runtime(node_type="preview", handler=self._runtime_preview)

    def execute(
        self,
        plan: ExecutionPlan,
        requests: tuple[RepresentationRequest, ...],
        context: LocalExecutionContext,
        *,
        output_path: Path | None = None,
        checkpoint_path: Path | None = None,
        resume_from: Path | None = None,
    ) -> GenerationResult:
        if (checkpoint_path is not None or resume_from is not None) and context.cache is None:
            raise InvalidRangeError("checkpoint/resume 必须同时配置 cache_root")
        source_paths = tuple(dict.fromkeys(
            Path(request.source.uri).resolve(strict=True) for request in requests
        ))
        source_sha_by_path = {
            path: sha256_file(path) for path in source_paths
        }
        request_sha256 = hashlib.sha256(canonical_json([
            request.model_dump(mode="json") for request in requests
        ]).encode("utf-8")).hexdigest()
        input_sha256 = hashlib.sha256(canonical_json([
            {"source_id": request.source.source_id,
             "sha256": source_sha_by_path[Path(request.source.uri).resolve(strict=True)]}
            for request in requests
        ]).encode("utf-8")).hexdigest()
        operator_versions = {
            node.operator_name: node.operator_version for node in plan.nodes
        }
        completed_nodes: set[str] = set()
        completed_chunks_by_node: dict[str, set[int]] = {}
        completed_chunk_hashes: dict[str, dict[int, str]] = {}
        checkpoint_target = checkpoint_path or resume_from
        if resume_from is not None:
            record, state = context.load_checkpoint(
                resume_from,
                plan_id=plan.plan_id,
                request_sha256=request_sha256,
                input_sha256=input_sha256,
                operator_versions=operator_versions,
            )
            try:
                state_data = json.loads(state)
                completed_nodes = set(state_data["completed_node_ids"])
                completed_chunks_by_node = {
                    str(node_id): {int(position) for position in positions}
                    for node_id, positions in state_data.get("completed_chunks", {}).items()
                }
                completed_chunk_hashes = {
                    str(node_id): {
                        int(position): str(digest)
                        for position, digest in hashes.items()
                    }
                    for node_id, hashes in state_data.get("chunk_sha256", {}).items()
                }
            except Exception as exc:
                raise CheckpointIncompatibleError("checkpoint 状态无效") from exc
        values: dict[str, object] = {}
        stores: dict[Path, SharedFrameStore] = {}
        content_hashes: dict[str, str] = {}
        cache_hits = 0
        cache_writes = 0
        source_records: list[SourceRecord] = []
        source_policies: dict[str, str] = {}
        source_paths: dict[str, Path] = {}
        for request in requests:
            resolved_source = Path(request.source.uri).resolve(strict=True)
            existing_path = source_paths.setdefault(
                request.source.source_id, resolved_source,
            )
            if existing_path != resolved_source:
                raise InvalidRangeError(
                    f"同一 source_id 指向不同文件：{request.source.source_id}"
                )
            policy = request.output.metadata_policy
            existing = source_policies.setdefault(request.source.source_id, policy)
            if existing != policy:
                raise InvalidRangeError(
                    f"同一来源 {request.source.source_id} 的 metadata_policy 冲突"
                )
        has_bundle_output = any(
            request.output.format in {"bundle", "zarr"} for request in requests
        )
        npy_output_indexes = tuple(
            index for index, request in enumerate(requests)
            if request.output.format == "npy"
        )
        if (has_bundle_output or npy_output_indexes) and output_path is None:
            raise InvalidRangeError("NPY/Bundle/Zarr 输出必须提供 output_path")
        if len(npy_output_indexes) > 1:
            raise InvalidRangeError("一次执行最多只能指定一个独立 NPY 输出")
        if has_bundle_output and npy_output_indexes:
            raise InvalidRangeError("独立 NPY 与 Bundle/Zarr 不能共用同一 output_path")
        succeeded = False
        try:
            for node in plan.nodes:
                context.ensure_active()
                config = json.loads(node.config_json)
                inputs = tuple(values[item] for item in node.input_node_ids)
                if node.node_type == "source":
                    path = Path(config["uri"]).resolve(strict=True)
                    values[node.node_id] = path
                    source_id = str(config["source_id"])
                    stat = path.stat()
                    source_sha256 = source_sha_by_path[path]
                    content_hashes[node.node_id] = source_sha256
                    source_records.append(SourceRecord(
                        source_id=source_id,
                        media_identity=MediaIdentity(
                            source_id=source_id, size_bytes=stat.st_size,
                            sha256=source_sha256, modified_time_ns=stat.st_mtime_ns,
                            file_id=f"{stat.st_dev:x}:{stat.st_ino:x}",
                            actual_format=detect_actual_format(path),
                        ),
                        original_uri=(
                            str(path) if source_policies[source_id] == "full" else None
                        ),
                        metadata_policy=source_policies[source_id],
                    ))
                elif node.node_type == "decode":
                    path = inputs[0]
                    assert isinstance(path, Path)
                    store = stores.get(path)
                    if store is None:
                        store = SharedFrameStore(path, context)
                        stores[path] = store
                    values[node.node_id] = store
                elif node.node_type in {"sample", "transform", "reduce", "frequency", "preview"}:
                    input_hash = self._combined_hash(
                        tuple(content_hashes[item] for item in node.input_node_ids)
                    )
                    keys = self._cache_keys(
                        node, config, inputs, input_hash,
                        plan.execution_semantics_version,
                    )
                    cached = self._load_cached(context, keys)
                    if node.node_id in completed_nodes and cached is None:
                        raise CheckpointIncompatibleError(
                            f"checkpoint 对应缓存已缺失：{node.node_id}"
                        )
                    if cached is not None:
                        values[node.node_id] = cached
                        cache_hits += len(cached)
                        self._remove_partial(self._partial_path(
                            context, request_sha256, node.node_id,
                            enabled=checkpoint_target is not None,
                        ))
                    else:
                        computed = self._execute_compute_node(
                            node, inputs, config, context,
                            file_backed=(
                                has_bundle_output
                                or bool(npy_output_indexes)
                                or context.cache is not None
                            ),
                            partial_path=self._partial_path(
                                context, request_sha256, node.node_id,
                                enabled=checkpoint_target is not None,
                            ),
                            completed_positions=completed_chunks_by_node.get(
                                node.node_id, set(),
                            ),
                            completed_hashes=completed_chunk_hashes.get(
                                node.node_id, {},
                            ),
                            on_chunk_completed=(
                                lambda position, digest, node_id=node.node_id: self._record_chunk(
                                    context, checkpoint_target, plan,
                                    request_sha256, input_sha256,
                                    operator_versions, completed_nodes,
                                    completed_chunks_by_node, completed_chunk_hashes,
                                    node_id, position, digest,
                                )
                                if checkpoint_target is not None else None
                            ),
                        )
                        values[node.node_id] = computed
                        stored = self._store_cached(context, keys, computed)
                        if stored is not None:
                            values[node.node_id] = stored
                            cache_writes += len(stored)
                            self._remove_partial(
                                self._partial_path(
                                    context, request_sha256, node.node_id,
                                    enabled=checkpoint_target is not None,
                                )
                            )
                    if keys and isinstance(values[node.node_id], tuple):
                        completed_nodes.add(node.node_id)
                        if checkpoint_target is not None:
                            self._write_checkpoint(
                                context, checkpoint_target, plan,
                                request_sha256, input_sha256,
                                operator_versions, completed_nodes,
                                completed_chunks_by_node,
                                completed_chunk_hashes,
                            )
                elif node.node_type == "artifact":
                    values[node.node_id] = self._as_tensors(
                        inputs[0], context=context, node_id=node.node_id,
                        file_backed=(has_bundle_output or bool(npy_output_indexes)),
                    )
                else:
                    raise InvalidRangeError(f"未知 DAG 节点类型：{node.node_type}")
                if node.node_id not in content_hashes:
                    parent_hashes = tuple(
                        content_hashes[item] for item in node.input_node_ids
                    )
                    content_hashes[node.node_id] = self._semantic_hash(
                        node, parent_hashes,
                    )

            request_tensors = tuple(
                tuple(tensor for output in output_ids for tensor in self._as_tensors(values[output]))
                for output_ids in plan.outputs
            )
            for index, output_ids in enumerate(plan.outputs):
                context.report_progress(output_ids[0], index + 1, len(requests))
            unique: dict[tuple[str, str], TensorField] = {}
            for tensor in (item for group in request_tensors for item in group):
                node_id = str(tensor.attributes.get("dag_node_id", ""))
                unique.setdefault((node_id, tensor.tensor_id), tensor)
            bundle = None
            result_plan = plan
            self._verify_sources_unchanged(source_sha_by_path)
            if npy_output_indexes:
                assert output_path is not None
                request_index = npy_output_indexes[0]
                data_tensors = tuple(
                    tensor for tensor in request_tensors[request_index]
                    if tensor.attributes.get("artifact_role") != "preview"
                )
                if len(data_tensors) != 1:
                    raise InvalidRangeError("独立 NPY 输出必须恰好产生一个 Data Tensor")
                source_tensor = data_tensors[0]
                persisted_dtype = np.dtype(source_tensor.data.dtype).newbyteorder("<").str
                handle = save_array_handle_npy(
                    source_tensor.data,
                    output_path,
                    target_dtype=persisted_dtype,
                )
                replacement = TensorField(
                    tensor_id=source_tensor.tensor_id,
                    data=handle,
                    axes=source_tensor.axes,
                    channels=source_tensor.channels,
                    coordinate_space=source_tensor.coordinate_space,
                    axis_mappings=source_tensor.axis_mappings,
                    validity=source_tensor.validity,
                    accuracy=source_tensor.accuracy,
                    provenance=source_tensor.provenance,
                    attributes=source_tensor.attributes,
                )
                old_groups = request_tensors
                request_tensors = tuple(
                    tuple(
                        replacement if (
                            group_index == request_index
                            and tensor is source_tensor
                        ) else tensor
                        for tensor in group
                    )
                    for group_index, group in enumerate(request_tensors)
                )
                self._close_replaced_handles(old_groups, request_tensors)
            if has_bundle_output:
                assert output_path is not None
                array_format = "zarr" if any(request.output.format == "zarr" for request in requests) else "npy"
                bundle = BundleWriter().write(
                    output_path, tuple(unique.values()), requests=requests,
                    sources=tuple(source_records), array_format=array_format,
                    execution_plan=plan.model_dump(mode="json"),
                    events=tuple(context.events),
                    execution_id=context.execution_id,
                )
                # safe metadata policy 会脱敏 plan 中 source 节点的 config_json。
                # 对外返回与 Bundle 同一份可复现、无本机路径泄露的 ExecutionPlan。
                plan_artifact_id = bundle.manifest.execution_summary.get(
                    "plan_artifact_id"
                )
                if not isinstance(plan_artifact_id, str):
                    raise InvalidRangeError("Bundle 缺少 execution_plan Artifact")
                plan_record = bundle.artifact(plan_artifact_id)
                result_plan = ExecutionPlan.model_validate_json(
                    (bundle.root / plan_record.storage.files[0].uri).read_bytes()
                )
                persistent = {
                    (str(record.attributes.get("dag_node_id", "")), record.role):
                    bundle.open_tensor(record.artifact_id)
                    for record in bundle.manifest.artifacts
                    if record.kind == "data"
                }
                original_tensors = request_tensors
                request_tensors = tuple(
                    tuple(persistent.get((
                        str(tensor.attributes.get("dag_node_id", "")),
                        tensor.tensor_id,
                    ), tensor) for tensor in group)
                    for group in request_tensors
                )
                self._close_replaced_handles(original_tensors, request_tensors)
            result = GenerationResult(
                plan=result_plan, request_tensors=request_tensors, bundle=bundle,
                events=tuple(context.events),
                decode_passes=sum(store.decode_passes for store in stores.values()),
                cache_hits=cache_hits,
                cache_writes=cache_writes,
            )
            succeeded = True
            return result
        finally:
            for store in stores.values():
                store.close()
            if not succeeded:
                closed: set[int] = set()
                for value in values.values():
                    if not (
                        isinstance(value, tuple)
                        and all(isinstance(item, TensorField) for item in value)
                    ):
                        continue
                    for tensor in value:
                        handle_id = id(tensor.data)
                        if handle_id in closed:
                            continue
                        close = getattr(tensor.data, "close", None)
                        if callable(close):
                            close()
                        closed.add(handle_id)

    def _execute_compute_node(
        self,
        node: PlannedNode,
        inputs: tuple[object, ...],
        config: dict[str, object],
        context: LocalExecutionContext,
        *,
        file_backed: bool,
        partial_path: Path | None,
        completed_positions: set[int],
        completed_hashes: dict[int, str],
        on_chunk_completed: Callable[[int, str], None] | None,
    ) -> tuple[TensorField, ...]:
        graph_node = GraphNode(
            node_id=node.node_id,
            node_type=node.node_type,  # type: ignore[arg-type]
            operator_name=node.operator_name,
            operator_version=node.operator_version,
            config_json=node.config_json,
            input_node_ids=node.input_node_ids,
        )
        planning = self.registry.resolve(graph_node)
        runtime = self.registry.resolve_runtime(graph_node)
        validated_config = planning.validate_config(config)
        def compute(
            input_chunks: tuple[Iterator[TensorChunk], ...],
        ) -> tuple[TensorField, ...]:
            invocation = RuntimeInvocation(
                node_id=node.node_id,
                node_type=node.node_type,
                operator_name=node.operator_name,
                operator_version=node.operator_version,
                inputs=inputs,
                input_chunks=input_chunks,
                config=config,
                context=context,
                file_backed=file_backed,
                partial_path=partial_path,
                completed_positions=frozenset(completed_positions),
                completed_hashes=completed_hashes,
                on_chunk_completed=on_chunk_completed,
            )
            return self._annotate_outputs(node, inputs, runtime(invocation))

        operator = planning.bind(compute)
        input_chunks = tuple(
            self._value_chunks(value, context.resources.preferred_chunk_bytes)
            for value in inputs
        )
        chunks = operator.execute(
            context, input_chunks, validated_config, node.operator_plan,
        )
        return operator.finalize(context, chunks, node.operator_plan)

    @staticmethod
    def _value_chunks(
        value: object, preferred_chunk_bytes: int,
    ) -> Iterator[TensorChunk]:
        """把上游 Tensor 输出转换成 Operator 可消费的有界流。"""
        if not (
            isinstance(value, tuple)
            and all(isinstance(item, TensorField) for item in value)
        ):
            return iter(())

        def stream() -> Iterator[TensorChunk]:
            for tensor_position, tensor in enumerate(value):
                shape = tensor.data.shape
                if not shape:
                    selections = ((),)
                else:
                    row_bytes = max(
                        1,
                        int(np.prod(shape[1:], dtype=np.int64))
                        * np.dtype(tensor.data.dtype).itemsize,
                    )
                    rows = max(1, preferred_chunk_bytes // row_bytes)
                    selections = tuple(
                        (slice(start, min(start + rows, shape[0])),)
                        + tuple(slice(None) for _ in shape[1:])
                        for start in range(0, shape[0], rows)
                    )
                for chunk_position, selection in enumerate(selections):
                    validity = None
                    if tensor.validity is not None:
                        validity = tensor.validity.read(selection)
                    yield TensorChunk(
                        tensor_id=tensor.tensor_id,
                        data=tensor.data.read(selection),
                        read_selection=selection,
                        core_selection=selection,
                        axis_mappings=tensor.axis_mappings,
                        validity=validity,
                        chunk_index=(tensor_position, chunk_position),
                    )

        return stream()

    @staticmethod
    def _annotate_outputs(
        node: PlannedNode,
        inputs: tuple[object, ...],
        outputs: tuple[TensorField, ...],
    ) -> tuple[TensorField, ...]:
        input_tensor_ids = tuple(
            tensor.tensor_id
            for value in inputs
            for tensor in (value if isinstance(value, tuple) else ())
            if isinstance(tensor, TensorField)
        )
        return tuple(replace(tensor, attributes={
            **tensor.attributes,
            "dag_node_id": node.node_id,
            "dag_input_node_ids": list(node.input_node_ids),
            "input_tensor_ids": list(input_tensor_ids),
            "operator": node.operator_name,
            "operator_version": node.operator_version,
            "operator_config": json.loads(node.config_json),
            "runtime_dependencies": LocalExecutor._runtime_dependencies(
                node, json.loads(node.config_json), inputs,
            ),
        }) for tensor in outputs)

    def _runtime_sample(
        self, invocation: RuntimeInvocation,
    ) -> tuple[TensorField, ...]:
        context = invocation.context
        if not isinstance(context, LocalExecutionContext):
            raise InvalidRangeError("内置采样算子需要 LocalExecutionContext")
        store = invocation.inputs[0]
        if not isinstance(store, SharedFrameStore):
            raise InvalidRangeError("采样算子必须接收 SharedFrameStore")
        if store.frames is None:
            store.decode()
        return self._execute_sample_node(
            invocation.node_id,
            store,
            invocation.config,
            context,
            file_backed=invocation.file_backed,
            partial_path=invocation.partial_path,
            completed_positions=set(invocation.completed_positions),
            completed_hashes=dict(invocation.completed_hashes),
            on_chunk_completed=invocation.on_chunk_completed,
        )

    def _runtime_transform(
        self, invocation: RuntimeInvocation,
    ) -> tuple[TensorField, ...]:
        if invocation.operator_name in {
            "feature.grayscale", "feature.hsv", "feature.lab",
        }:
            return self._execute_color_conversion(invocation)
        if invocation.operator_name in {"feature.diff", "feature.frame_difference"}:
            return self._execute_frame_difference(invocation)
        return self._execute_transform_node(
            invocation.operator_name,
            invocation.inputs[0],
            invocation.config,
            invocation.context.resources.max_memory_bytes,
        )

    def _execute_color_conversion(
        self, invocation: RuntimeInvocation,
    ) -> tuple[TensorField, ...]:
        context = invocation.context
        if not isinstance(context, LocalExecutionContext):
            raise InvalidRangeError("颜色转换需要 LocalExecutionContext")
        tensors = self._as_tensors(invocation.inputs[0])
        if len(tensors) != 1:
            raise InvalidRangeError("颜色转换必须接收一个帧 Tensor")
        source = tensors[0]
        if len(source.data.shape) != 4 or source.data.shape[-1] != 3:
            raise InvalidRangeError("颜色转换必须接收 RGB 帧 Tensor")
        model = invocation.operator_name.removeprefix("feature.")
        options = dict(invocation.config.get("config") or {})
        weights = tuple(float(item) for item in options.get(
            "weights", (0.299, 0.587, 0.114),
        ))

        def read(position: int) -> np.ndarray:
            frame = source.data.read((
                position, slice(None), slice(None), slice(None),
            ))
            if model == "grayscale":
                return rgb_to_grayscale(frame, weights)  # type: ignore[arg-type]
            if model == "hsv":
                return rgb_to_hsv(frame)
            return rgb_to_lab(frame)

        positions = tuple(range(source.data.shape[0]))
        handle = _rows_to_handle(
            invocation.node_id, positions, read, context,
            file_backed=invocation.file_backed,
            partial_path=invocation.partial_path,
            completed_positions=set(invocation.completed_positions),
            completed_hashes=dict(invocation.completed_hashes),
            on_chunk_completed=invocation.on_chunk_completed,
        )
        accuracy = AccuracyInfo(
            level=AccuracyLevel.DERIVED,
            source=f"feature.{model}:1.0.0",
            assumptions=((
                "D65 white point"
                if model == "lab"
                else "sRGB code values"
                if model == "hsv"
                else f"weights={weights}"
            ),),
        )
        tensor_id = f"tensor_frames_{model}"
        mapping = tuple(item.model_copy(update={
            "output_artifact_id": tensor_id,
        }) for item in source.axis_mappings)
        if model == "grayscale":
            axes = source.axes[:-1]
            channels: tuple[ChannelSpec, ...] = ()
        else:
            axes = source.axes
            names = (
                ("hue", "saturation", "value")
                if model == "hsv" else ("lightness", "a", "b")
            )
            units = (
                ("degree", "ratio", "ratio")
                if model == "hsv" else (None, None, None)
            )
            channels = tuple(ChannelSpec(
                name=name, semantic=name, unit=unit, accuracy=accuracy,
            ) for name, unit in zip(names, units))
        return (replace(
            source, tensor_id=tensor_id, data=handle, axes=axes,
            channels=channels, axis_mappings=mapping, accuracy=accuracy,
            provenance=ProvenanceRef(provenance_id=f"prov_{model}"),
            attributes={
                **source.attributes, "color_model": model,
                "color_conversion": options,
            },
        ),)

    def _execute_frame_difference(
        self, invocation: RuntimeInvocation,
    ) -> tuple[TensorField, ...]:
        context = invocation.context
        if not isinstance(context, LocalExecutionContext):
            raise InvalidRangeError("Frame Difference 需要 LocalExecutionContext")
        tensors = self._as_tensors(invocation.inputs[0])
        if len(tensors) != 1:
            raise InvalidRangeError("Frame Difference 必须接收一个帧 Tensor")
        source = tensors[0]
        if len(source.data.shape) != 4 or source.data.shape[-1] != 3:
            raise InvalidRangeError("Frame Difference 必须接收 RGB 帧 Tensor")
        count = source.data.shape[0]
        if count < 2:
            raise InvalidRangeError("Frame Difference 至少需要两帧")
        indices = tuple(int(item) for item in source.attributes.get(
            "presentation_indices", (),
        ))
        times = tuple(float(item) for item in source.attributes.get(
            "timeline_timestamps_seconds", (),
        ))
        if len(indices) != count or len(times) != count:
            raise InvalidRangeError("帧 Tensor 缺少完整时间映射")
        divide = bool(dict(invocation.config.get("config") or {}).get(
            "divide_by_delta_time", False,
        ))

        def read(position: int) -> np.ndarray:
            previous = source.data.read((
                position - 1, slice(None), slice(None), slice(None),
            )).astype(np.int16)
            current = source.data.read((
                position, slice(None), slice(None), slice(None),
            )).astype(np.int16)
            difference = np.abs(current - previous)
            if not divide:
                return difference.astype(np.uint8)
            delta = times[position] - times[position - 1]
            if delta <= 0:
                raise InvalidRangeError(
                    "divide_by_delta_time 要求严格递增的时间戳"
                )
            return difference.astype(np.float64) / delta

        positions = tuple(range(1, count))
        handle = _rows_to_handle(
            invocation.node_id, positions, read, context,
            file_backed=invocation.file_backed,
            partial_path=invocation.partial_path,
            completed_positions=set(invocation.completed_positions),
            completed_hashes=dict(invocation.completed_hashes),
            on_chunk_completed=invocation.on_chunk_completed,
        )
        tensor = _frames_tensor(
            handle, indices[1:], times[1:],
            nominal_fps=source.attributes.get("nominal_fps"),
        )
        accuracy = AccuracyInfo(
            level=AccuracyLevel.DERIVED,
            source="feature.frame_difference:1.0.0",
            unit="code_value/second" if divide else "code_value",
        )
        time_axis = tensor.axes[0].model_copy(
            update={"mapping_id": "map_tensor_frame_difference_time"},
        )
        time_mapping = tensor.axis_mappings[0].model_copy(update={
            "mapping_id": "map_tensor_frame_difference_time",
            "output_artifact_id": "tensor_frame_difference",
            "parameters": {
                **tensor.axis_mappings[0].parameters,
                "frame_pairs": [
                    list(pair) for pair in zip(indices[:-1], indices[1:])
                ],
            },
            "accuracy": accuracy,
        })
        return (replace(
            tensor,
            tensor_id="tensor_frame_difference",
            axes=(time_axis, *tensor.axes[1:]),
            channels=_rgb_channels(accuracy),
            axis_mappings=(time_mapping,),
            accuracy=accuracy,
            provenance=ProvenanceRef(provenance_id="prov_frame_difference"),
            attributes={
                **tensor.attributes,
                "divide_by_delta_time": divide,
                "frame_pairs": [
                    list(pair) for pair in zip(indices[:-1], indices[1:])
                ],
            },
        ),)

    def _runtime_frequency(
        self, invocation: RuntimeInvocation,
    ) -> tuple[TensorField, ...]:
        tensors = self._as_tensors(invocation.inputs[0])
        if len(tensors) != 1:
            raise InvalidRangeError("频域 Operator 必须接收一个帧 Tensor")
        return self._execute_frequency_node(
            invocation.operator_name, tensors[0], invocation.config,
            invocation.context.resources.max_memory_bytes,
        )

    def _runtime_reduce(
        self, invocation: RuntimeInvocation,
    ) -> tuple[TensorField, ...]:
        context = invocation.context
        tensors = self._as_tensors(invocation.inputs[0])
        if len(tensors) != 1:
            raise InvalidRangeError("时间聚合必须接收一个帧 Tensor")
        source = tensors[0]
        checkpoint_handle: ArrayHandle | None = None
        if invocation.partial_path is not None:
            positions = tuple(range(source.data.shape[0]))

            def read(position: int) -> np.ndarray:
                return source.data.read((
                    position, *(slice(None) for _ in source.data.shape[1:]),
                ))

            checkpoint_handle = _rows_to_handle(
                invocation.node_id,
                positions,
                read,
                context,
                file_backed=True,
                partial_path=invocation.partial_path,
                completed_positions=set(invocation.completed_positions),
                completed_hashes=dict(invocation.completed_hashes),
                on_chunk_completed=invocation.on_chunk_completed,
            )
            source = replace(source, data=checkpoint_handle)
        try:
            return (_reduce(
                source,
                invocation.operator_name.removeprefix("reduce."),
                context.resources.max_memory_bytes,
                dict(invocation.config.get("config") or {}),
            ),)
        finally:
            if checkpoint_handle is not None:
                close = getattr(checkpoint_handle, "close", None)
                if callable(close):
                    close()

    def _runtime_preview(
        self, invocation: RuntimeInvocation,
    ) -> tuple[TensorField, ...]:
        context = invocation.context
        tensors = self._as_tensors(invocation.inputs[0])
        config = invocation.config
        preview_mode = config.get("mode", "auto")
        if preview_mode in {"flow_direction", "flow_magnitude"}:
            source = tensors[-2] if preview_mode == "flow_direction" else tensors[-1]
            raw_values = tensors[0].data.materialize(
                max_bytes=context.resources.max_memory_bytes,
            )
            metadata = tensors[-1].attributes
            effective_flow = reconstruct_effective_flow(
                raw_values,
                compensated=bool(metadata.get("compensated", False)),
                global_motion=metadata.get("global_motion"),
            )
            image = (
                flow_direction_preview(effective_flow)
                if preview_mode == "flow_direction"
                else flow_magnitude_preview(np.hypot(
                    effective_flow[..., 0], effective_flow[..., 1],
                ))
            )
            return (make_preview_tensor(
                image,
                tensor_id=f"preview_{preview_mode}",
                source_tensor_id=source.tensor_id,
                source_width=image.shape[1],
                source_height=image.shape[0],
                attributes={"visualization": preview_mode},
            ),)
        if preview_mode in {"temporal_fft", "spatial_fft", "stft"}:
            source = tensors[-1]
            if preview_mode == "temporal_fft":
                image = temporal_fft_preview(source)
            elif preview_mode == "spatial_fft":
                image = spatial_fft_preview(source)
            else:
                magnitude = np.log1p(np.abs(source.data.materialize(
                    max_bytes=context.resources.max_memory_bytes,
                ))).T
                high = float(magnitude.max()) if magnitude.size else 0.0
                gray = (
                    np.zeros(magnitude.shape, dtype=np.uint8)
                    if high <= 0
                    else np.clip(magnitude * 255.0 / high, 0, 255).astype(np.uint8)
                )
                image = np.repeat(gray[..., None], 3, axis=2)
            return (make_preview_tensor(
                image,
                tensor_id=f"preview_{preview_mode}",
                source_tensor_id=source.tensor_id,
                source_width=(
                    max(source.data.shape[0] - 1, 1)
                    if preview_mode == "temporal_fft" else image.shape[1]
                ),
                source_height=1 if preview_mode == "temporal_fft" else image.shape[0],
                attributes={
                    "visualization": (
                        "magnitude_curve" if preview_mode == "temporal_fft"
                        else "log_magnitude"
                    ),
                },
            ),)
        return (_preview(
            tensors[-1], context.resources.max_memory_bytes, config,
        ),)

    @staticmethod
    def _partial_path(
        context: LocalExecutionContext,
        request_sha256: str,
        node_id: str,
        *,
        enabled: bool,
    ) -> Path | None:
        if not enabled or context.cache is None:
            return None
        return context.cache.partials / request_sha256 / node_id / "data.npy"

    @staticmethod
    def _remove_partial(path: Path | None) -> None:
        if path is not None and path.parent.exists():
            shutil.rmtree(path.parent)

    @classmethod
    def _record_chunk(
        cls,
        context: LocalExecutionContext,
        target: Path | None,
        plan: ExecutionPlan,
        request_sha256: str,
        input_sha256: str,
        operator_versions: dict[str, str],
        completed_nodes: set[str],
        completed_chunks_by_node: dict[str, set[int]],
        completed_chunk_hashes: dict[str, dict[int, str]],
        node_id: str,
        position: int,
        digest: str,
    ) -> None:
        if target is None:
            return
        completed_chunks_by_node.setdefault(node_id, set()).add(position)
        completed_chunk_hashes.setdefault(node_id, {})[position] = digest
        cls._write_checkpoint(
            context, target, plan, request_sha256, input_sha256,
            operator_versions, completed_nodes, completed_chunks_by_node,
            completed_chunk_hashes,
        )

    @staticmethod
    def _write_checkpoint(
        context: LocalExecutionContext,
        target: Path,
        plan: ExecutionPlan,
        request_sha256: str,
        input_sha256: str,
        operator_versions: dict[str, str],
        completed_nodes: set[str],
        completed_chunks_by_node: dict[str, set[int]],
        completed_chunk_hashes: dict[str, dict[int, str]],
    ) -> None:
        ordered = tuple(
            node.node_id for node in plan.nodes if node.node_id in completed_nodes
        )
        node_indexes = {
            node.node_id: index for index, node in enumerate(plan.nodes)
        }
        state = canonical_json({
            "completed_node_ids": ordered,
            "completed_chunks": {
                node.node_id: sorted(completed_chunks_by_node.get(node.node_id, set()))
                for node in plan.nodes
                if completed_chunks_by_node.get(node.node_id)
            },
            "chunk_sha256": {
                node.node_id: {
                    str(position): completed_chunk_hashes[node.node_id][position]
                    for position in sorted(completed_chunks_by_node.get(node.node_id, set()))
                }
                for node in plan.nodes
                if completed_chunks_by_node.get(node.node_id)
            },
        }).encode("utf-8")
        context.checkpoint_to(Path(target), CheckpointRecord(
            plan_id=plan.plan_id,
            request_sha256=request_sha256,
            input_sha256=input_sha256,
            operator_versions=operator_versions,
            completed_chunks=tuple(
                (node_indexes[node.node_id], position)
                for node in plan.nodes
                for position in sorted(completed_chunks_by_node.get(node.node_id, set()))
            ),
            state_base64=encoded_state(state),
        ))

    @staticmethod
    def _combined_hash(values: tuple[str, ...]) -> str:
        return hashlib.sha256("\0".join(values).encode("ascii")).hexdigest()

    @staticmethod
    def _verify_sources_unchanged(source_sha_by_path: dict[Path, str]) -> None:
        for path, expected_sha256 in source_sha_by_path.items():
            try:
                actual_sha256 = sha256_file(path)
            except OSError as exc:
                raise MediaChangedDuringAnalysisError(
                    f"分析期间媒体文件不可再读取：{path}"
                ) from exc
            if actual_sha256 != expected_sha256:
                raise MediaChangedDuringAnalysisError(
                    f"分析期间媒体内容发生变化：{path}"
                )

    @staticmethod
    def _semantic_hash(node: PlannedNode, parent_hashes: tuple[str, ...]) -> str:
        payload = "\0".join((
            node.operator_name, node.operator_version, node.config_json,
            *parent_hashes,
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _cache_dtypes(
        node: PlannedNode, config: dict[str, object],
    ) -> tuple[str, ...]:
        del config
        if not node.cacheable:
            return ()
        return tuple(
            descriptor.dtype for descriptor in node.operator_plan.output_descriptors
        )

    def _cache_keys(
        self,
        node: PlannedNode,
        config: dict[str, object],
        inputs: tuple[object, ...],
        input_hash: str,
        semantics: str,
    ) -> tuple[CacheKeyInput, ...]:
        descriptors = tuple(self._value_descriptor(value) for value in inputs)
        cache_config = {
            **config,
            "runtime_dependencies": self._runtime_dependencies(node, config, inputs),
        }
        return tuple(
            CacheKeyInput(
                input_content_sha256=input_hash,
                operator_name=node.operator_name,
                operator_version=node.operator_version,
                canonical_config=cache_config,
                input_tensor_descriptors=descriptors,
                dtype=dtype,
                precision="decoded" if dtype == "uint8" else "derived",
                execution_semantics_version=semantics,
                artifact_role=f"output_{index}",
            )
            for index, dtype in enumerate(self._cache_dtypes(node, config))
        )

    @staticmethod
    def _runtime_dependencies(
        node: PlannedNode,
        config: dict[str, object] | None = None,
        inputs: tuple[object, ...] = (),
    ) -> dict[str, str]:
        del inputs
        dependencies = {"numpy": np.__version__}
        if node.node_type == "sample":
            try:
                dependencies["av"] = package_version("av")
            except PackageNotFoundError:
                dependencies["av"] = "unknown"
        preview_mode = (config or {}).get("mode")
        if (
            node.operator_name in {"feature.flow", "feature.farneback"}
            or preview_mode in {"flow_direction", "flow_magnitude"}
        ):
            dependencies["opencv"] = str(require_cv2().__version__)
        return dependencies

    @staticmethod
    def _value_descriptor(value: object) -> dict[str, object]:
        if isinstance(value, SharedFrameStore):
            return {
                "kind": "decoded_media",
                "pixel_format": "rgb24",
                "presentation_order": True,
            }
        if isinstance(value, tuple):
            return {
                "kind": "tensor_collection",
                "tensors": [
                    {
                        "dtype": item.data.dtype,
                        "shape": list(item.data.shape),
                        "axes": [axis.model_dump(mode="json") for axis in item.axes],
                        "channels": [
                            channel.model_dump(mode="json") for channel in item.channels
                        ],
                        "coordinate_space": (
                            item.coordinate_space.model_dump(mode="json")
                            if item.coordinate_space is not None else None
                        ),
                    }
                    for item in value if isinstance(item, TensorField)
                ],
            }
        return {"kind": type(value).__name__}

    @staticmethod
    def _load_cached(
        context: LocalExecutionContext,
        keys: tuple[CacheKeyInput, ...],
    ) -> tuple[TensorField, ...] | None:
        if context.cache is None or not keys:
            return None
        restored: list[TensorField] = []
        for key in keys:
            tensor = context.cache.get_tensor(key)
            for warning in context.cache.pop_warnings():
                context.events.append({
                    "type": "warning",
                    "code": warning.code,
                    "message": str(warning),
                })
            if tensor is None:
                for item in restored:
                    item.data.close()  # type: ignore[attr-defined]
                return None
            restored.append(tensor)
        return tuple(restored)

    @staticmethod
    def _store_cached(
        context: LocalExecutionContext,
        keys: tuple[CacheKeyInput, ...],
        value: tuple[TensorField, ...],
    ) -> tuple[TensorField, ...] | None:
        if context.cache is None or not keys:
            return None
        if len(keys) != len(value):
            raise InvalidRangeError("缓存输出数量与 Operator 输出不一致")
        stored = tuple(
            context.cache.put_tensor(key, tensor)
            for key, tensor in zip(keys, value)
        )
        for tensor in value:
            close = getattr(tensor.data, "close", None)
            if callable(close):
                close()
        return stored

    @staticmethod
    def _as_tensors(
        value: object,
        *,
        context: LocalExecutionContext | None = None,
        node_id: str = "artifact.frames",
        file_backed: bool = False,
    ) -> tuple[TensorField, ...]:
        if isinstance(value, tuple) and all(isinstance(item, TensorField) for item in value):
            return value
        raise InvalidRangeError("DAG 节点没有产生 TensorField")

    def _execute_sample_node(
        self,
        node_id: str,
        store: SharedFrameStore,
        config: dict[str, object],
        context: LocalExecutionContext,
        *,
        file_backed: bool,
        partial_path: Path | None,
        completed_positions: set[int],
        completed_hashes: dict[int, str],
        on_chunk_completed: Callable[[int, str], None] | None,
    ) -> tuple[TensorField, ...]:
        selection = TemporalSelection.model_validate(config["selection"])
        indices = store.selection_indices(selection)
        times = tuple(store.times[index] for index in indices)
        representation = str(config["representation"])
        if representation == "frames":
            assert store.frames is not None
            handle = _rows_to_handle(
                node_id,
                indices,
                lambda index: store.frames.read(
                    (index, slice(None), slice(None), slice(None))
                ),
                context,
                file_backed=file_backed,
                partial_path=partial_path,
                completed_positions=completed_positions,
                completed_hashes=completed_hashes,
                on_chunk_completed=on_chunk_completed,
            )
            return (_frames_tensor(
                handle, indices, times, nominal_fps=store.fps,
            ),)
        geometry = TypeAdapter(Geometry).validate_python(config["geometry"])
        sampling_plan = self._sampling_plan(
            store, representation, geometry,
            dict(config.get("feature_config") or {}),
            dict(config.get("sampling_reduction") or {}), indices,
        )
        assert store.frames is not None
        handle = _rows_to_handle(
            node_id,
            indices,
            lambda index: _sample_frame(
                store.frames.read((index, slice(None), slice(None), slice(None))),
                sampling_plan,
            ),
            context,
            file_backed=file_backed,
            partial_path=partial_path,
            completed_positions=completed_positions,
            completed_hashes=completed_hashes,
            on_chunk_completed=on_chunk_completed,
        )
        return (_build_tensor(handle, sampling_plan, indices, times),)

    @staticmethod
    def _close_replaced_handles(
        original: tuple[tuple[TensorField, ...], ...],
        replacement: tuple[tuple[TensorField, ...], ...],
    ) -> None:
        retained = {id(tensor.data) for group in replacement for tensor in group}
        closed: set[int] = set()
        for tensor in (item for group in original for item in group):
            handle_id = id(tensor.data)
            if handle_id in retained or handle_id in closed:
                continue
            close = getattr(tensor.data, "close", None)
            if callable(close):
                close()
                closed.add(handle_id)

    @staticmethod
    def _execute_transform_node(
        operator_name: str,
        value: object,
        config: dict[str, object],
        max_memory_bytes: int,
    ) -> tuple[TensorField, ...]:
        if operator_name not in {"feature.flow", "feature.farneback"}:
            raise InvalidRangeError(f"1.0 暂不支持 Feature：{config.get('name')}")
        tensors = LocalExecutor._as_tensors(value)
        if len(tensors) != 1:
            raise InvalidRangeError("Farneback 必须接收一个帧 Tensor")
        tensor = tensors[0]
        if tensor.data.shape[-1:] != (3,):
            raise InvalidRangeError("Farneback 必须接收 RGB 帧 Tensor")
        presentation_indices = tuple(
            int(item) for item in tensor.attributes.get("presentation_indices", ())
        )
        if len(presentation_indices) != tensor.data.shape[0]:
            raise InvalidRangeError("帧 Tensor 缺少完整 presentation_index 映射")
        position_by_index = {
            presentation_index: position
            for position, presentation_index in enumerate(presentation_indices)
        }
        options = dict(config.get("config") or {})
        pixel_count = int(np.prod(tensor.data.shape[1:3], dtype=np.int64))
        explicit_working_bytes = pixel_count * (
            96 if options.get("compensate_global") else 64
        )
        if explicit_working_bytes > max_memory_bytes:
            raise MaterializationLimitExceededError(
                f"Farneback 完整分辨率工作集至少需要 {explicit_working_bytes} 字节，"
                f"超过限制 {max_memory_bytes} 字节"
            )
        accumulate = bool(options.get("accumulate", False))
        pair_config = options.get("frame_pair")
        flow_indices = (
            tuple(int(item) for item in pair_config)
            if pair_config is not None else presentation_indices
        )
        if len(flow_indices) < 2 or (not accumulate and len(flow_indices) != 2):
            raise InvalidRangeError(
                "累积 Farneback 至少需要两帧" if accumulate
                else "Farneback 请求必须恰好选择两个展示帧"
            )
        cv2 = require_cv2()
        try:
            flow_positions = tuple(position_by_index[index] for index in flow_indices)
        except KeyError as exc:
            raise InvalidRangeError(
                f"Farneback 请求的展示帧不在输入 Tensor 中：{exc.args[0]}"
            ) from exc
        previous = _gray(tensor.data.read((
            flow_positions[0], slice(None), slice(None), slice(None),
        )))
        flow: np.ndarray | None = None
        for position in flow_positions[1:]:
            current = _gray(tensor.data.read((
                position, slice(None), slice(None), slice(None),
            )))
            pair = _farneback(cv2, previous, current)
            flow = pair if flow is None else flow + pair
            previous = current
            if not accumulate:
                break
        assert flow is not None
        return build_flow_tensors(
            flow,
            frame_a=flow_indices[0],
            frame_b=flow_indices[-1],
            frames_analyzed=len(flow_indices),
            accumulated=accumulate,
            compensate_global=bool(options.get("compensate_global", False)),
            mag_threshold=float(options.get("mag_threshold", 1.0)),
        )

    @staticmethod
    def _execute_frequency_node(
        operator_name: str,
        value: TensorField,
        config: dict[str, object],
        max_memory_bytes: int,
    ) -> tuple[TensorField, ...]:
        if value.data.shape[-1:] != (3,):
            raise InvalidRangeError("频域 Operator 必须接收 RGB 帧 Tensor")
        indices = tuple(
            int(item) for item in value.attributes.get("presentation_indices", ())
        )
        times = tuple(
            float(item)
            for item in value.attributes.get("timeline_timestamps_seconds", ())
        )
        if len(indices) != value.data.shape[0] or len(times) != value.data.shape[0]:
            raise InvalidRangeError("帧 Tensor 缺少完整时间与 presentation_index 映射")
        source_width = value.coordinate_space.width if value.coordinate_space else None
        source_height = value.coordinate_space.height if value.coordinate_space else None
        if source_width is None or source_height is None:
            raise InvalidRangeError("帧 Tensor 缺少存储坐标尺寸")
        options = dict(config.get("config") or {})
        if operator_name in {"feature.temporal_fft", "feature.stft"}:
            sample_count = value.data.shape[0]
            if operator_name == "feature.stft" and all(
                key in options for key in ("length", "hop", "padding")
            ):
                length = int(options["length"])
                hop = int(options["hop"])
                padding = str(options["padding"])
                padded_count = sample_count + (length - 1 if padding == "center" else 0)
                if padding == "none":
                    window_count = max(0, 1 + (padded_count - length) // hop)
                else:
                    window_count = max(1, (max(padded_count - length, 0) + hop - 1) // hop + 1)
                output_bytes = window_count * (length // 2 + 1) * 16
            else:
                output_bytes = (sample_count // 2 + 1) * 16
            required_bytes = output_bytes * 2 + sample_count * 8
            if required_bytes > max_memory_bytes:
                raise MaterializationLimitExceededError(
                    f"{operator_name} 精确工作集至少需要 {required_bytes} 字节，"
                    f"超过限制 {max_memory_bytes} 字节"
                )
            source = str(options.get("source", "luma"))
            if source not in {"luma", "change"}:
                raise InvalidRangeError("source 无效，可选 change/luma")
            rect_value = options.get("rect")
            point_value = options.get("point")
            if rect_value is not None and point_value is not None:
                raise InvalidRangeError("rect 与 point 最多只能指定一个")
            rect = tuple(int(item) for item in rect_value) if rect_value is not None else None
            point = tuple(int(item) for item in point_value) if point_value is not None else None
            from pixelprobe.utils.coordinates import validate_point, validate_rect
            if rect is not None:
                validate_rect(*rect, source_width, source_height)
            if point is not None:
                validate_point(point[0], point[1], source_width, source_height)
            values: list[float] = []
            previous: np.ndarray | None = None
            for position in range(value.data.shape[0]):
                frame = value.data.read(
                    (position, slice(None), slice(None), slice(None)),
                )
                if rect is not None:
                    x, y, width, height = rect
                    frame = frame[y:y + height, x:x + width, :]
                elif point is not None:
                    x, y = point
                    frame = frame[y:y + 1, x:x + 1, :]
                if source == "luma":
                    luma = (
                        0.299 * frame[..., 0].astype(np.float64)
                        + 0.587 * frame[..., 1].astype(np.float64)
                        + 0.114 * frame[..., 2].astype(np.float64)
                    )
                    values.append(float(luma.mean()))
                else:
                    if previous is not None:
                        difference = np.maximum(frame, previous) - np.minimum(frame, previous)
                        values.append(float(difference.mean()))
                    previous = frame
            aligned_times = times[1:] if source == "change" else times
            try:
                if operator_name == "feature.stft":
                    required = {
                        "window", "length", "hop", "padding", "normalization",
                    }
                    missing = required.difference(options)
                    if missing:
                        raise ValueError(
                            "STFT 缺少参数：" + ", ".join(sorted(missing))
                        )
                    return (stft_from_series(
                        values,
                        aligned_times,
                        source=source,
                        window=str(options["window"]),
                        length=int(options["length"]),
                        hop=int(options["hop"]),
                        padding=str(options["padding"]),
                        normalization=str(options["normalization"]),
                    ),)
                return (temporal_fft_from_series(
                    values,
                    aligned_times,
                    source=source,
                    nominal_fps=(
                        float(value.attributes["nominal_fps"])
                        if value.attributes.get("nominal_fps") is not None else None
                    ),
                    sample_every=int(options.get("sample_every", 1)),
                    allow_vfr_estimate=(
                        str(options.get("vfr_policy", "error")) == "estimate"
                    ),
                ),)
            except ValueError as exc:
                raise InvalidRangeError(str(exc)) from exc
        if operator_name == "feature.spatial_fft":
            if len(indices) != 1:
                raise InvalidRangeError("空间 FFT 必须恰好选择一帧")
            rect_value = options.get("rect")
            rect = tuple(int(item) for item in rect_value) if rect_value is not None else None
            if rect is not None:
                from pixelprobe.utils.coordinates import validate_rect
                validate_rect(*rect, source_width, source_height)
            frame = value.data.read(
                (0, slice(None), slice(None), slice(None)),
            )
            fft_height = rect[3] if rect is not None else frame.shape[0]
            fft_width = rect[2] if rect is not None else frame.shape[1]
            required_bytes = fft_height * fft_width * (16 * 2 + 8)
            if required_bytes > max_memory_bytes:
                raise MaterializationLimitExceededError(
                    f"空间 FFT 精确工作集至少需要 {required_bytes} 字节，"
                    f"超过限制 {max_memory_bytes} 字节"
                )
            try:
                return (spatial_fft_from_frame(
                    frame,
                    source_width=source_width,
                    source_height=source_height,
                    rect=rect,
                    frame_index=indices[0],
                    time_seconds=times[0],
                ),)
            except ValueError as exc:
                raise InvalidRangeError(str(exc)) from exc
        raise InvalidRangeError(f"暂不支持频域 Operator：{operator_name}")

    @staticmethod
    def _sampling_plan(
        store: SharedFrameStore,
        representation: str,
        geometry: Geometry,
        feature_config: dict[str, object],
        sampling_reduction: dict[str, object],
        indices: tuple[int, ...],
    ) -> SamplingPlan:
        frame_range = FrameRange(indices[0], indices[-1], 1)
        if representation in {"xt", "yt"}:
            assert isinstance(geometry, PathGeometry)
            axis = 1 if representation == "xt" else 0
            fixed = int(round(geometry.points[0][axis]))
            return SamplingPlan(
                representation, frame_range, store.width, store.height,
                fixed_coordinate=fixed,
            )
        if representation == "path_t":
            assert isinstance(geometry, PathGeometry)
            points = geometry.points
            default_count = max(2, int(round(sum(
                np.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(points, points[1:])
            ))) + 1)
            sample_count = int(feature_config.get("sample_count", default_count))
            interpolation = str(feature_config.get("interpolation", "bilinear"))
            return SamplingPlan(
                "path_t", frame_range, store.width, store.height,
                points=resample_polyline(points, sample_count),
                interpolation=interpolation,
            )
        if representation == "points_t":
            if isinstance(geometry, PointGeometry):
                points = ((geometry.x, geometry.y),)
            else:
                assert isinstance(geometry, PathGeometry)
                points = geometry.points
            return SamplingPlan(
                "points_t", frame_range, store.width, store.height,
                points=points,
                block_size=(
                    int(feature_config["block_size"])
                    if feature_config.get("block_size") is not None else None
                ),
            )
        if representation == "roi_t":
            assert isinstance(geometry, RectGeometry)
            rect = tuple(int(round(value)) for value in (
                geometry.x, geometry.y, geometry.width, geometry.height,
            ))
            return SamplingPlan(
                "roi_t", frame_range, store.width, store.height,
                rect=rect,
                reduction=str(sampling_reduction.get("name", "mean")),
                percentile=dict(sampling_reduction.get("config") or {}).get("percentile"),
            )
        raise InvalidRangeError(f"暂不支持表示：{representation}")
