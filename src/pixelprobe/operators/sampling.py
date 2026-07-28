"""统一时空采样：X-T、Y-T、点集、Path-T 与 ROI-T。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.core.frame_selector import FrameRange, resolve_range
from pixelprobe.core.video_reader import VideoReader
from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace, CoordinateSpaceKind
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import ArrayHandle, MemoryArrayHandle, TensorField
from pixelprobe.models.errors import DecodeError, InvalidRangeError
from pixelprobe.operators.base import OperatorSpec
from pixelprobe.operators.common import memory_array_ref
from pixelprobe.utils.coordinates import validate_point, validate_rect

ProgressCallback = Callable[[int, int], None]
SamplingKind = Literal["xt", "yt", "points_t", "path_t", "roi_t"]
ReductionName = Literal["mean", "min", "max", "median", "std", "rms", "percentile"]

SAMPLING_OPERATOR_SPEC = OperatorSpec(
    name="sample.spatial_temporal",
    version="1.0.0",
    category="sample",
    deterministic="tolerance",
    stateful=False,
    chunkable=True,
    cacheable=True,
    supported_dtypes=("uint8", "float32", "float64"),
    config_schema_id="pixelprobe.operator.sample.v1",
)


class SamplingConfig(BaseModel):
    """采样算子的公开、可生成 JSON Schema 的配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SamplingKind
    interpolation: Literal["nearest", "bilinear"] = "nearest"
    boundary: Literal["error", "clip"] = "error"
    block_size: int | None = Field(default=None, ge=1)
    reduction: ReductionName | None = None
    percentile: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_combination(self) -> "SamplingConfig":
        if self.kind == "roi_t" and self.reduction is None:
            raise ValueError("ROI-T 必须提供 reduction")
        if self.kind != "roi_t" and self.reduction is not None:
            raise ValueError("只有 ROI-T 可以设置 reduction")
        if self.reduction == "percentile" and self.percentile is None:
            raise ValueError("percentile reduction 必须提供 percentile")
        if self.reduction != "percentile" and self.percentile is not None:
            raise ValueError("只有 percentile reduction 可以设置 percentile")
        if self.kind != "points_t" and self.block_size is not None:
            raise ValueError("只有 points_t 可以设置 block_size")
        return self


@dataclass(slots=True, frozen=True)
class SamplingPlan:
    kind: SamplingKind
    frame_range: FrameRange
    width: int
    height: int
    points: tuple[tuple[float, float], ...] = ()
    fixed_coordinate: int | None = None
    rect: tuple[int, int, int, int] | None = None
    interpolation: Literal["nearest", "bilinear"] = "nearest"
    boundary: Literal["error", "clip"] = "error"
    block_size: int | None = None
    reduction: ReductionName | None = None
    percentile: float | None = None

    def __post_init__(self) -> None:
        self.config
        if self.kind == "xt":
            if self.fixed_coordinate is None or not 0 <= self.fixed_coordinate < self.height:
                raise ValueError("X-T 的 y 坐标越界")
        elif self.kind == "yt":
            if self.fixed_coordinate is None or not 0 <= self.fixed_coordinate < self.width:
                raise ValueError("Y-T 的 x 坐标越界")
        elif self.kind in {"points_t", "path_t"} and not self.points:
            raise ValueError(f"{self.kind} 至少需要一个采样点")
        elif self.kind == "roi_t" and (self.rect is None or self.reduction is None):
            raise ValueError("ROI-T 必须提供 rect 和 reduction")

    @property
    def config(self) -> SamplingConfig:
        return SamplingConfig(
            kind=self.kind,
            interpolation=self.interpolation,
            boundary=self.boundary,
            block_size=self.block_size,
            reduction=self.reduction,
            percentile=self.percentile,
        )


@dataclass(slots=True, frozen=True)
class SamplingOutput:
    tensor: TensorField
    plan: SamplingPlan
    frames: tuple[int, ...]
    times: tuple[float, ...]


def resample_polyline(
    points: Sequence[tuple[float, float]],
    sample_count: int,
) -> tuple[tuple[float, float], ...]:
    """按弧长等距重采样折线，包含首尾点。"""
    if len(points) < 2:
        raise InvalidRangeError("Path-T 路径至少需要两个点")
    if sample_count < 2:
        raise InvalidRangeError("Path-T sample_count 必须 >= 2")
    array = np.asarray(points, dtype=np.float64)
    lengths = np.hypot(*(np.diff(array, axis=0).T))
    cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
    if cumulative[-1] <= 0:
        raise InvalidRangeError("Path-T 路径总长度必须大于 0")
    targets = np.linspace(0.0, cumulative[-1], sample_count)
    xs = np.interp(targets, cumulative, array[:, 0])
    ys = np.interp(targets, cumulative, array[:, 1])
    return tuple((float(x), float(y)) for x, y in zip(xs, ys))


def _sample_points(frame: np.ndarray, plan: SamplingPlan) -> np.ndarray:
    coords = np.asarray(plan.points, dtype=np.float64)
    xs, ys = coords[:, 0], coords[:, 1]
    if plan.boundary == "error":
        if (xs < 0).any() or (xs > plan.width - 1).any() or (ys < 0).any() or (ys > plan.height - 1).any():
            raise InvalidRangeError("采样点超出存储像素中心范围")
    else:
        xs = np.clip(xs, 0, plan.width - 1)
        ys = np.clip(ys, 0, plan.height - 1)

    if plan.block_size is not None:
        values = []
        for x, y in zip(xs, ys):
            ix, iy = int(round(x)), int(round(y))
            block = frame[
                iy : min(iy + plan.block_size, plan.height),
                ix : min(ix + plan.block_size, plan.width),
                :,
            ]
            values.append(block.reshape(-1, frame.shape[2]).mean(axis=0))
        return np.asarray(values, dtype=np.float64)

    if plan.interpolation == "nearest":
        ix = np.floor(xs + 0.5).astype(np.intp)
        iy = np.floor(ys + 0.5).astype(np.intp)
        return frame[iy, ix, :].copy()

    x0 = np.floor(xs).astype(np.intp)
    y0 = np.floor(ys).astype(np.intp)
    x1 = np.minimum(x0 + 1, plan.width - 1)
    y1 = np.minimum(y0 + 1, plan.height - 1)
    wx = (xs - x0)[:, None]
    wy = (ys - y0)[:, None]
    top = frame[y0, x0].astype(np.float64) * (1 - wx) + frame[y0, x1] * wx
    bottom = frame[y1, x0].astype(np.float64) * (1 - wx) + frame[y1, x1] * wx
    return (top * (1 - wy) + bottom * wy).astype(np.float32)


def _reduce_roi(frame: np.ndarray, plan: SamplingPlan) -> np.ndarray:
    assert plan.rect is not None and plan.reduction is not None
    x, y, width, height = plan.rect
    values = frame[y : y + height, x : x + width, :].astype(np.float64)
    axes = (0, 1)
    if plan.reduction == "mean":
        return values.mean(axis=axes)
    if plan.reduction == "min":
        return values.min(axis=axes)
    if plan.reduction == "max":
        return values.max(axis=axes)
    if plan.reduction == "median":
        return np.median(values, axis=axes)
    if plan.reduction == "std":
        return values.std(axis=axes)
    if plan.reduction == "rms":
        return np.sqrt(np.mean(values * values, axis=axes))
    assert plan.percentile is not None
    return np.percentile(values, plan.percentile, axis=axes)


def _sample_frame(frame: np.ndarray, plan: SamplingPlan) -> np.ndarray:
    if plan.kind == "xt":
        assert plan.fixed_coordinate is not None
        return frame[plan.fixed_coordinate, :, :].copy()
    if plan.kind == "yt":
        assert plan.fixed_coordinate is not None
        return frame[:, plan.fixed_coordinate, :].copy()
    if plan.kind in {"points_t", "path_t"}:
        return _sample_points(frame, plan)
    return _reduce_roi(frame, plan)


def _channels(accuracy: AccuracyInfo) -> tuple[ChannelSpec, ...]:
    return tuple(
        ChannelSpec(
            name=name,
            unit="code_value",
            semantic=semantic,
            value_range=(0, 255),
            accuracy=accuracy,
        )
        for name, semantic in (
            ("r", "decoded_srgb_red"),
            ("g", "decoded_srgb_green"),
            ("b", "decoded_srgb_blue"),
        )
    )


def _build_tensor(
    array: np.ndarray | ArrayHandle,
    plan: SamplingPlan,
    frames: tuple[int, ...],
    times: tuple[float, ...],
) -> TensorField:
    data = MemoryArrayHandle(array) if isinstance(array, np.ndarray) else array
    shape = data.shape
    tensor_id = f"tensor_{plan.kind}_rgb"
    time_values = np.asarray(times, dtype=np.float64)
    time_ref = memory_array_ref(time_values, f"index_{tensor_id}_time")
    time_axis = AxisSpec(
        name="time",
        kind=AxisKind.TIME,
        length=len(times),
        unit="second",
        coordinate_mode="irregular",
        coordinates_ref=time_ref,
        mapping_id=f"map_{tensor_id}_time",
    )
    axes: list[AxisSpec] = [time_axis]
    mappings: list[AxisMapping] = [
        AxisMapping(
            mapping_id=f"map_{tensor_id}_time",
            kind="lookup",
            input_artifact_id="source_media",
            input_axes=("time",),
            output_artifact_id=tensor_id,
            output_axes=("time",),
            parameters={"coordinates_ref": time_ref.artifact_id},
            accuracy=AccuracyInfo(
                level=AccuracyLevel.DECODED,
                source="pyav_pts",
                unit="second",
            ),
        )
    ]
    index_values: dict[str, object] = {
        time_ref.artifact_id: time_values.tolist(),
    }

    if plan.kind in {"xt", "yt"}:
        axis_name = "x" if plan.kind == "xt" else "y"
        axis_kind = AxisKind.X if plan.kind == "xt" else AxisKind.Y
        axes.append(AxisSpec(
            name=axis_name,
            kind=axis_kind,
            length=shape[1],
            unit="pixel",
            coordinate_mode="regular",
            start=0.0,
            step=1.0,
            mapping_id=f"map_{tensor_id}_{axis_name}",
        ))
        mappings.append(AxisMapping(
            mapping_id=f"map_{tensor_id}_{axis_name}",
            kind="affine",
            input_artifact_id="source_media",
            input_axes=(axis_name,),
            output_artifact_id=tensor_id,
            output_axes=(axis_name,),
            parameters={"scale": 1.0, "offset": 0.0},
            accuracy=AccuracyInfo(
                level=AccuracyLevel.EXACT,
                source="integer_pixel_index",
                unit="pixel",
            ),
        ))
    elif plan.kind in {"points_t", "path_t"}:
        coordinates = np.asarray(plan.points, dtype=np.float64)
        point_ref = memory_array_ref(coordinates, f"index_{tensor_id}_points")
        index_values[point_ref.artifact_id] = coordinates.tolist()
        axes.append(AxisSpec(
            name="path",
            kind=AxisKind.PATH,
            length=shape[1],
            unit="pixel",
            coordinate_mode="index",
            mapping_id=f"map_{tensor_id}_path",
        ))
        mappings.append(AxisMapping(
            mapping_id=f"map_{tensor_id}_path",
            kind="lookup",
            input_artifact_id="source_media",
            input_axes=("y", "x"),
            output_artifact_id=tensor_id,
            output_axes=("path",),
            parameters={"coordinates_ref": point_ref.artifact_id},
            accuracy=AccuracyInfo(
                level=(
                    AccuracyLevel.DECODED
                    if plan.interpolation == "nearest" and plan.block_size is None
                    else AccuracyLevel.DERIVED
                ),
                source=f"{plan.interpolation}_sampling",
                assumptions=("pixel-center coordinates",),
                unit="pixel",
            ),
        ))
    else:
        assert plan.rect is not None
        x, y, roi_width, roi_height = plan.rect
        starts = np.asarray((y, x), dtype=np.int64)
        ends = np.asarray((y + roi_height, x + roi_width), dtype=np.int64)
        starts_ref = memory_array_ref(starts, f"index_{tensor_id}_roi_starts")
        ends_ref = memory_array_ref(ends, f"index_{tensor_id}_roi_ends")
        index_values[starts_ref.artifact_id] = starts.tolist()
        index_values[ends_ref.artifact_id] = ends.tolist()
        mappings.append(AxisMapping(
            mapping_id=f"map_{tensor_id}_roi",
            kind="interval",
            input_artifact_id="source_media",
            input_axes=("y", "x"),
            output_artifact_id=tensor_id,
            output_axes=("time",),
            parameters={
                "starts_ref": starts_ref.artifact_id,
                "ends_ref": ends_ref.artifact_id,
            },
            accuracy=AccuracyInfo(
                level=AccuracyLevel.DERIVED,
                source=f"roi_{plan.reduction}",
                unit="pixel",
            ),
        ))

    axes.append(AxisSpec(
        name="channel",
        kind=AxisKind.CHANNEL,
        length=3,
        coordinate_mode="index",
    ))
    derived = plan.kind == "roi_t" or plan.interpolation == "bilinear" or plan.block_size is not None
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DERIVED if derived else AccuracyLevel.DECODED,
        source=f"{SAMPLING_OPERATOR_SPEC.name}:{SAMPLING_OPERATOR_SPEC.version}",
        assumptions=((f"interpolation={plan.interpolation}",) if derived else ()),
        unit="code_value",
    )
    return TensorField(
        tensor_id=tensor_id,
        data=data,
        axes=tuple(axes),
        channels=_channels(accuracy),
        coordinate_space=CoordinateSpace(
            coordinate_space_id="storage_pixels",
            kind=CoordinateSpaceKind.STORAGE,
            axes=("x", "y"),
            width=plan.width,
            height=plan.height,
        ),
        axis_mappings=tuple(mappings),
        validity=None,
        accuracy=accuracy,
        provenance=ProvenanceRef(
            provenance_id=f"prov_{SAMPLING_OPERATOR_SPEC.name}_{plan.kind}"
        ),
        attributes={
            "representation": plan.kind,
            "sample_count": int(shape[1]) if len(shape) == 3 else 1,
            "presentation_indices": list(frames),
            "timeline_timestamps_seconds": list(times),
            "fixed_coordinate": plan.fixed_coordinate,
            "rect": plan.rect,
            "_index_values": index_values,
        },
    )


def execute_sampling(
    reader: VideoReader,
    plan: SamplingPlan,
    progress: ProgressCallback | None = None,
) -> SamplingOutput:
    rows: list[np.ndarray] = []
    frames: list[int] = []
    times: list[float] = []
    for packet in reader.iter_frame_packets(
        plan.frame_range.start,
        plan.frame_range.end,
        plan.frame_range.sample_every,
    ):
        rows.append(_sample_frame(packet.data, plan))
        frames.append(packet.presentation_index)
        times.append(packet.timeline_time_seconds)
        if progress is not None:
            progress(len(rows), plan.frame_range.count)
    if not rows:
        raise DecodeError("指定范围内没有解码出任何帧")
    array = np.stack(rows, axis=0)
    return SamplingOutput(
        tensor=_build_tensor(array, plan, tuple(frames), tuple(times)),
        plan=plan,
        frames=tuple(frames),
        times=tuple(times),
    )


def _run(
    path: Path,
    builder: Callable[[int, int, FrameRange], SamplingPlan],
    *,
    start_frame: int | None = None,
    end_frame: int | None = None,
    start: float | None = None,
    end: float | None = None,
    sample_every: int = 1,
    progress: ProgressCallback | None = None,
) -> SamplingOutput:
    with VideoReader() as reader:
        reader.open(Path(path))
        info = reader.get_info()
        frame_range = resolve_range(
            reader, start_frame, end_frame, start, end, sample_every,
        )
        plan = builder(info.width, info.height, frame_range)
        return execute_sampling(reader, plan, progress)


def sample_xt(path: Path, y: int, **range_options: object) -> SamplingOutput:
    def builder(width: int, height: int, frame_range: FrameRange) -> SamplingPlan:
        validate_point(0, y, width, height)
        return SamplingPlan("xt", frame_range, width, height, fixed_coordinate=y)
    return _run(path, builder, **range_options)  # type: ignore[arg-type]


def sample_yt(path: Path, x: int, **range_options: object) -> SamplingOutput:
    def builder(width: int, height: int, frame_range: FrameRange) -> SamplingPlan:
        validate_point(x, 0, width, height)
        return SamplingPlan("yt", frame_range, width, height, fixed_coordinate=x)
    return _run(path, builder, **range_options)  # type: ignore[arg-type]


def sample_points_t(
    path: Path,
    points: Sequence[tuple[int, int]],
    *,
    block_size: int | None = None,
    **range_options: object,
) -> SamplingOutput:
    def builder(width: int, height: int, frame_range: FrameRange) -> SamplingPlan:
        for x, y in points:
            validate_point(x, y, width, height)
        return SamplingPlan(
            "points_t", frame_range, width, height,
            points=tuple((float(x), float(y)) for x, y in points),
            block_size=block_size,
        )
    return _run(path, builder, **range_options)  # type: ignore[arg-type]


def sample_path_t(
    path: Path,
    points: Sequence[tuple[float, float]],
    *,
    sample_count: int,
    interpolation: Literal["nearest", "bilinear"] = "bilinear",
    boundary: Literal["error", "clip"] = "error",
    **range_options: object,
) -> SamplingOutput:
    sampled = resample_polyline(points, sample_count)

    def builder(width: int, height: int, frame_range: FrameRange) -> SamplingPlan:
        return SamplingPlan(
            "path_t", frame_range, width, height,
            points=sampled,
            interpolation=interpolation,
            boundary=boundary,
        )
    return _run(path, builder, **range_options)  # type: ignore[arg-type]


def sample_roi_t(
    path: Path,
    rect: tuple[int, int, int, int],
    *,
    reduction: ReductionName = "mean",
    percentile: float | None = None,
    **range_options: object,
) -> SamplingOutput:
    def builder(width: int, height: int, frame_range: FrameRange) -> SamplingPlan:
        validate_rect(*rect, width, height)
        return SamplingPlan(
            "roi_t", frame_range, width, height,
            rect=rect,
            reduction=reduction,
            percentile=percentile,
        )
    return _run(path, builder, **range_options)  # type: ignore[arg-type]
