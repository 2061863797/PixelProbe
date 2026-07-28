"""DAG 节点的配置验证、符号类型推导与资源规划注册表。"""

from __future__ import annotations

import hashlib
from itertools import chain
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisSpec, ChannelSpec
from pixelprobe.domain.geometry import Geometry
from pixelprobe.domain.media import MediaSource
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.engine.graph import GraphNode, canonical_json
from pixelprobe.engine.errors import (
    ChunkMappingMismatchError,
    OperatorConfigInvalidError,
    OperatorExecutionError,
    OperatorNotRegisteredError,
    OperatorTypeMismatchError,
    ResourcePlanUnsatisfiableError,
)
from pixelprobe.engine.request import FeatureRequest, ReductionRequest
from pixelprobe.operators.base import (
    ExecutionContext,
    Operator,
    OperatorPlan,
    OperatorSpec,
    ResourcePolicy,
    RuntimeInvocation,
    TensorDescriptor,
    TensorChunk,
)
from pixelprobe.domain.tensor import TensorField
from pixelprobe.operators.optical_flow import FLOW_OPERATOR_SPEC
from pixelprobe.operators.frequency import FREQUENCY_OPERATOR_SPEC
from pixelprobe.operators.preview import PREVIEW_OPERATOR_SPEC
from pixelprobe.operators.reduction import REDUCTION_OPERATOR_SPEC
from pixelprobe.operators.sampling import SAMPLING_OPERATOR_SPEC
from pixelprobe.operators.transforms import (
    COLOR_CONVERSION_OPERATOR_SPEC,
    FRAME_DIFFERENCE_OPERATOR_SPEC,
)


class DecodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sample_semantics: Literal["decoded_sample"]
    presentation_order: bool


class SampleNodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    representation: Literal["frames", "xt", "yt", "points_t", "path_t", "roi_t"]
    selection: TemporalSelection
    geometry: Geometry | None
    feature_config: dict[str, object]
    sampling_reduction: dict[str, object] | None = None


class PreviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal[
        "auto", "temporal_reduce", "flow_direction", "flow_magnitude",
        "temporal_fft", "spatial_fft", "stft",
    ] = "auto"
    p_low: float = 1.0
    p_high: float = 99.0
    destripe: bool = False
    smooth: int = 0


class ArtifactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal["memory", "npy", "zarr", "bundle"]
    role: Literal["data", "preview"]


class FrameDifferenceOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    divide_by_delta_time: bool = False


class GrayscaleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    weights: tuple[float, float, float] = (0.299, 0.587, 0.114)


class EmptyColorOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FlowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    accumulate: bool = False
    compensate_global: bool = False
    mag_threshold: float = Field(default=1.0, ge=0)
    frame_pair: tuple[int, int] | None = None


class TemporalFrequencyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: Literal["luma", "change"] = "luma"
    rect: tuple[int, int, int, int] | None = None
    point: tuple[int, int] | None = None
    sample_every: int = Field(default=1, ge=1)
    vfr_policy: Literal["error", "estimate"] = "error"


class SpatialFrequencyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rect: tuple[int, int, int, int] | None = None
    report_image_semantics: bool = False


class StftOptions(TemporalFrequencyOptions):
    window: Literal["hann", "hamming", "rect"]
    length: int = Field(ge=2)
    hop: int = Field(ge=1)
    padding: Literal["none", "end", "center"]
    normalization: Literal["none", "window_sum", "window_energy"]


ConfigValidator = Callable[[Mapping[str, object]], BaseModel]
InferOutput = Callable[
    [tuple[TensorDescriptor, ...], BaseModel], tuple[TensorDescriptor, ...]
]
RuntimeCompute = Callable[
    [tuple[Iterator[TensorChunk], ...]], tuple[TensorField, ...]
]
RuntimeHandler = Callable[[RuntimeInvocation], tuple[TensorField, ...]]


def _validated_feature(
    value: Mapping[str, object],
    *,
    names: set[str],
    options_model: type[BaseModel],
) -> FeatureRequest:
    request = FeatureRequest.model_validate(value)
    if request.name not in names:
        raise ValueError(f"Feature 名称与 Operator 不匹配：{request.name}")
    options = options_model.model_validate(request.config)
    return request.model_copy(update={"config": options.model_dump(mode="json")})


def _validate_flow(value: Mapping[str, object]) -> FeatureRequest:
    return _validated_feature(
        value, names={"flow", "farneback"}, options_model=FlowOptions,
    )


def _validate_frame_difference(value: Mapping[str, object]) -> FeatureRequest:
    return _validated_feature(
        value, names={"diff", "frame_difference"},
        options_model=FrameDifferenceOptions,
    )


def _validate_color_conversion(value: Mapping[str, object]) -> FeatureRequest:
    request = FeatureRequest.model_validate(value)
    model: type[BaseModel]
    if request.name == "grayscale":
        model = GrayscaleOptions
    elif request.name in {"hsv", "lab"}:
        model = EmptyColorOptions
    else:
        raise ValueError(f"暂不支持颜色 Feature：{request.name}")
    options = model.model_validate(request.config)
    if isinstance(options, GrayscaleOptions) and not np.isclose(
        sum(options.weights), 1.0, rtol=0, atol=1e-6,
    ):
        raise ValueError("grayscale weights 之和必须为 1")
    return request.model_copy(update={"config": options.model_dump(mode="json")})


def _validate_frequency(value: Mapping[str, object]) -> FeatureRequest:
    request = FeatureRequest.model_validate(value)
    model_by_name: dict[str, type[BaseModel]] = {
        "temporal_fft": TemporalFrequencyOptions,
        "spatial_fft": SpatialFrequencyOptions,
        "stft": StftOptions,
    }
    model = model_by_name.get(request.name)
    if model is None:
        raise ValueError(f"暂不支持频域 Feature：{request.name}")
    options = model.model_validate(request.config)
    return request.model_copy(update={"config": options.model_dump(mode="json")})


def _choose_chunk_shape(
    shape: tuple[int, ...], dtype: str, max_bytes: int,
) -> tuple[int, ...]:
    itemsize = np.dtype(dtype).itemsize
    if max_bytes < itemsize:
        raise ResourcePlanUnsatisfiableError("内存预算小于单个元素")
    chunk = list(shape)
    while int(np.prod(chunk, dtype=np.int64)) * itemsize > max_bytes:
        axis = max(range(len(chunk)), key=chunk.__getitem__)
        if chunk[axis] == 1:
            raise ResourcePlanUnsatisfiableError("无法在预算内规划合法 chunk")
        chunk[axis] = max(1, (chunk[axis] + 1) // 2)
    return tuple(chunk)


def _accuracy(level: AccuracyLevel = AccuracyLevel.DERIVED) -> AccuracyInfo:
    return AccuracyInfo(level=level, source="operator_symbolic_inference")


def _axis(name: str, kind: AxisKind, length: int = 0) -> AxisSpec:
    return AxisSpec(name=name, kind=kind, length=length)


def _channels(names: tuple[str, ...]) -> tuple[ChannelSpec, ...]:
    accuracy = _accuracy(AccuracyLevel.DECODED)
    return tuple(
        ChannelSpec(name=name, semantic=name, accuracy=accuracy) for name in names
    )


def _descriptor(
    dtype: str,
    shape: tuple[int | None, ...],
    axes: tuple[AxisSpec, ...],
    channels: tuple[ChannelSpec, ...] = (),
    coordinate_space_id: str | None = "storage_pixels",
) -> TensorDescriptor:
    return TensorDescriptor(
        dtype=dtype, shape=shape, axes=axes, channels=channels,
        coordinate_space_id=coordinate_space_id,
    )


def _frame_descriptor(time_length: int | None = None) -> TensorDescriptor:
    return _descriptor(
        "uint8", (time_length, None, None, 3),
        (
            _axis("time", AxisKind.TIME, time_length or 0),
            _axis("y", AxisKind.Y), _axis("x", AxisKind.X),
            _axis("channel", AxisKind.CHANNEL, 3),
        ),
        _channels(("r", "g", "b")),
    )


def _selection_length(selection: TemporalSelection) -> int | None:
    if selection.mode == "indices":
        return len(selection.requested_indices[::selection.sample_every])
    if selection.mode == "frame_interval":
        span = selection.requested_end_frame_exclusive - selection.requested_start_frame
        return (span + selection.sample_every - 1) // selection.sample_every
    return None


def _infer_none(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    return ()


def _infer_decode(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    return (_frame_descriptor(),)


def _infer_sample(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    assert isinstance(config, SampleNodeConfig)
    if len(inputs) != 1 or len(inputs[0].shape) != 4:
        raise ValueError("采样节点必须接收一个 [time,y,x,channel] Tensor")
    time_length = _selection_length(config.selection)
    if config.representation == "frames":
        return (_frame_descriptor(time_length),)
    time_axis = _axis("time", AxisKind.TIME, time_length or 0)
    channel_axis = _axis("channel", AxisKind.CHANNEL, 3)
    if config.representation == "xt":
        return (_descriptor(
            "uint8", (time_length, None, 3),
            (time_axis, _axis("x", AxisKind.X), channel_axis),
            _channels(("r", "g", "b")),
        ),)
    if config.representation == "yt":
        return (_descriptor(
            "uint8", (time_length, None, 3),
            (time_axis, _axis("y", AxisKind.Y), channel_axis),
            _channels(("r", "g", "b")),
        ),)
    if config.representation in {"points_t", "path_t"}:
        count = (
            config.feature_config.get("point_count")
            if config.representation == "points_t"
            else config.feature_config.get("sample_count")
        )
        path_length = int(count) if count is not None else None
        return (_descriptor(
            "uint8", (time_length, path_length, 3),
            (time_axis, _axis("path", AxisKind.PATH, path_length or 0), channel_axis),
            _channels(("r", "g", "b")), coordinate_space_id=None,
        ),)
    return (_descriptor(
        "float64", (time_length, 3), (time_axis, channel_axis),
        _channels(("r", "g", "b")), coordinate_space_id=None,
    ),)


def _infer_flow(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    if len(inputs) != 1 or len(inputs[0].shape) != 4:
        raise ValueError("Farneback 必须接收帧序列 Tensor")
    height, width = inputs[0].shape[1:3]
    axes = (_axis("y", AxisKind.Y), _axis("x", AxisKind.X))
    flow = _descriptor(
        "float32", (height, width, 2),
        (*axes, _axis("channel", AxisKind.CHANNEL, 2)),
        _channels(("flow_x", "flow_y")),
    )
    magnitude = _descriptor("float32", (height, width), axes)
    assert isinstance(config, FeatureRequest)
    return (flow, flow, magnitude) if config.config.get("compensate_global") else (flow, magnitude)


def _infer_frame_difference(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    if len(inputs) != 1 or len(inputs[0].shape) != 4:
        raise ValueError("Frame Difference 必须接收帧序列 Tensor")
    assert isinstance(config, FeatureRequest)
    source = inputs[0]
    time_length = source.shape[0]
    output_time = None if time_length is None else max(time_length - 1, 0)
    divide = bool(config.config.get("divide_by_delta_time", False))
    axes = (
        source.axes[0].model_copy(update={"length": output_time or 0}),
        *source.axes[1:],
    )
    return (_descriptor(
        "float64" if divide else source.dtype,
        (output_time, *source.shape[1:]),
        axes, source.channels, source.coordinate_space_id,
    ),)


def _infer_color_conversion(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    if len(inputs) != 1 or len(inputs[0].shape) != 4:
        raise ValueError("颜色转换必须接收帧序列 Tensor")
    assert isinstance(config, FeatureRequest)
    source = inputs[0]
    if config.name == "grayscale":
        return (_descriptor(
            "float32", source.shape[:-1], source.axes[:-1], (),
            source.coordinate_space_id,
        ),)
    channels = (
        _channels(("hue", "saturation", "value"))
        if config.name == "hsv" else _channels(("lightness", "a", "b"))
    )
    return (_descriptor(
        "float32", source.shape, source.axes, channels,
        source.coordinate_space_id,
    ),)


def _infer_reduce(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    if len(inputs) != 1 or len(inputs[0].shape) != 4:
        raise ValueError("时间聚合必须接收帧序列 Tensor")
    source = inputs[0]
    return (_descriptor(
        "float64", source.shape[1:], source.axes[1:], source.channels,
        source.coordinate_space_id,
    ),)


def _infer_frequency(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    assert isinstance(config, FeatureRequest)
    if config.name == "temporal_fft":
        return (_descriptor(
            "complex128", (None,), (_axis("frequency", AxisKind.FREQUENCY),),
            coordinate_space_id=None,
        ),)
    if config.name == "spatial_fft":
        return (_descriptor(
            "complex128", (None, None),
            (_axis("frequency_y", AxisKind.FREQUENCY),
             _axis("frequency_x", AxisKind.FREQUENCY)),
            coordinate_space_id="spatial_frequency",
        ),)
    if config.name == "stft":
        return (_descriptor(
            "complex128", (None, None),
            (_axis("window_time", AxisKind.TIME),
             _axis("frequency", AxisKind.FREQUENCY)),
            coordinate_space_id=None,
        ),)
    raise ValueError(f"暂不支持频域 Feature：{config.name}")


def _infer_preview(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    if not inputs:
        raise ValueError("Preview 缺少输入 Tensor")
    return (_descriptor(
        "uint8", (None, None, 3),
        (_axis("y", AxisKind.Y), _axis("x", AxisKind.X),
         _axis("channel", AxisKind.CHANNEL, 3)),
        _channels(("r", "g", "b")), "display_pixels",
    ),)


def _infer_passthrough(
    inputs: tuple[TensorDescriptor, ...], config: BaseModel,
) -> tuple[TensorDescriptor, ...]:
    return inputs


@dataclass(slots=True, frozen=True)
class PlanningOperator:
    spec: OperatorSpec
    validator: ConfigValidator
    infer: InferOutput

    def validate_config(self, config: Mapping[str, object]) -> BaseModel:
        try:
            return self.validator(config)
        except Exception as exc:
            raise OperatorConfigInvalidError(
                f"Operator 配置无效：{self.spec.name}（{exc}）"
            ) from exc

    def infer_output(
        self,
        inputs: tuple[TensorDescriptor, ...],
        config: BaseModel,
    ) -> tuple[TensorDescriptor, ...]:
        try:
            return self.infer(inputs, config)
        except Exception as exc:
            raise OperatorTypeMismatchError(
                f"Operator 输入类型不匹配：{self.spec.name}（{exc}）"
            ) from exc

    def plan(
        self,
        inputs: tuple[TensorDescriptor, ...],
        outputs: tuple[TensorDescriptor, ...],
        config: BaseModel,
        resources: ResourcePolicy,
    ) -> OperatorPlan:
        estimated = 0
        for output in outputs:
            known = tuple(length if length is not None else 1 for length in output.shape)
            estimated += (
                int(np.prod(known, dtype=np.int64)) * np.dtype(output.dtype).itemsize
            )
        peak = max(estimated, 1)
        first = outputs[0] if outputs else None
        chunk_shape = None
        chunk_axes: tuple[str, ...] = ()
        if self.spec.chunkable and first is not None:
            known_shape = tuple(
                max(1, length if length is not None else 1) for length in first.shape
            )
            chunk_shape = _choose_chunk_shape(
                known_shape,
                first.dtype,
                min(resources.max_memory_bytes, resources.preferred_chunk_bytes),
            )
            chunk_axes = tuple(axis.name for axis in first.axes)
            peak = (
                int(np.prod(chunk_shape, dtype=np.int64))
                * np.dtype(first.dtype).itemsize
            )
        elif peak > resources.max_memory_bytes:
            raise ResourcePlanUnsatisfiableError(
                f"非分块 Operator {self.spec.name} 至少需要 {peak} 字节，"
                f"超过内存预算 {resources.max_memory_bytes} 字节"
            )
        key_payload = canonical_json({
            "operator": self.spec.name,
            "version": self.spec.version,
            "config": config.model_dump(mode="json"),
            "inputs": [item.model_dump(mode="json") for item in inputs],
            "backend": "cpu",
        })
        return OperatorPlan(
            operator_name=self.spec.name,
            operator_version=self.spec.version,
            input_descriptors=inputs,
            output_descriptors=outputs,
            chunk_axes=chunk_axes,
            chunk_shape=chunk_shape,
            estimated_peak_memory_bytes=peak,
            estimated_temporary_bytes=0,
            cache_key=(
                hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
                if self.spec.cacheable else None
            ),
        )

    def bind(self, compute: RuntimeCompute) -> "BoundOperator":
        """将已经完成规划的算子绑定到单次本地执行。"""
        return BoundOperator(self, compute)


class BoundOperator(Operator):
    """单节点、单次使用的本地 CPU Operator 生命周期实现。"""

    def __init__(self, planning: PlanningOperator, compute: RuntimeCompute) -> None:
        self._planning = planning
        self._compute = compute
        self._outputs: tuple[TensorField, ...] | None = None
        self.spec = planning.spec

    def validate_config(self, config: Mapping[str, object]) -> BaseModel:
        return self._planning.validate_config(config)

    def infer_output(
        self,
        inputs: tuple[TensorDescriptor, ...],
        config: BaseModel,
    ) -> tuple[TensorDescriptor, ...]:
        return self._planning.infer_output(inputs, config)

    def plan(
        self,
        inputs: tuple[TensorDescriptor, ...],
        outputs: tuple[TensorDescriptor, ...],
        config: BaseModel,
        resources: ResourcePolicy,
    ) -> OperatorPlan:
        return self._planning.plan(inputs, outputs, config, resources)

    def execute(
        self,
        context: ExecutionContext,
        inputs: tuple[Iterator[TensorChunk], ...],
        config: BaseModel,
        plan: OperatorPlan,
    ) -> Iterator[TensorChunk]:
        del config
        if self._outputs is not None:
            raise OperatorExecutionError("同一个绑定 Operator 不能重复执行")
        self._outputs = self._compute(inputs)
        preferred = context.resources.preferred_chunk_bytes
        for output_index, tensor in enumerate(self._outputs):
            shape = tensor.data.shape
            if not shape:
                selections = ((),)
            else:
                row_bytes = max(
                    1,
                    int(np.prod(shape[1:], dtype=np.int64))
                    * np.dtype(tensor.data.dtype).itemsize,
                )
                rows = max(1, preferred // row_bytes)
                selections = tuple(
                    (slice(start, min(start + rows, shape[0])),)
                    + tuple(slice(None) for _ in shape[1:])
                    for start in range(0, shape[0], rows)
                )
            for chunk_position, selection in enumerate(selections):
                data = tensor.data.read(selection)
                yield TensorChunk(
                    tensor_id=tensor.tensor_id,
                    data=data,
                    read_selection=selection,
                    core_selection=selection,
                    axis_mappings=tensor.axis_mappings,
                    validity=None,
                    chunk_index=(output_index, chunk_position),
                )

    def finalize(
        self,
        context: ExecutionContext,
        chunks: Iterator[TensorChunk],
        plan: OperatorPlan,
    ) -> tuple[TensorField, ...]:
        del context, plan
        iterator = iter(chunks)
        try:
            first_chunk = next(iterator)
        except StopIteration:
            chunk_stream: Iterator[TensorChunk] = iter(())
        else:
            chunk_stream = chain((first_chunk,), iterator)
        if self._outputs is None:
            raise OperatorExecutionError("finalize 不能早于 execute")
        expected = {tensor.tensor_id: tensor for tensor in self._outputs}
        if len(expected) != len(self._outputs):
            raise ChunkMappingMismatchError("Operator 输出 tensor_id 必须唯一")
        offsets = {tensor_id: 0 for tensor_id in expected}
        seen_scalars: set[str] = set()
        for chunk in chunk_stream:
            tensor = expected.get(chunk.tensor_id)
            if tensor is None:
                raise ChunkMappingMismatchError(
                    f"发现未知输出 chunk：{chunk.tensor_id}"
                )
            shape = tensor.data.shape
            if not shape:
                if chunk.read_selection or chunk.tensor_id in seen_scalars:
                    raise ChunkMappingMismatchError("标量输出 chunk 覆盖无效")
                seen_scalars.add(chunk.tensor_id)
                continue
            selection = chunk.core_selection
            if len(selection) != len(shape):
                raise ChunkMappingMismatchError("chunk 维数与输出 Tensor 不一致")
            first = selection[0]
            start = 0 if first.start is None else first.start
            stop = shape[0] if first.stop is None else first.stop
            if first.step not in (None, 1) or start != offsets[chunk.tensor_id]:
                raise ChunkMappingMismatchError("chunk 在第一轴上不连续")
            if any(item != slice(None) for item in selection[1:]):
                raise ChunkMappingMismatchError("本地 Operator 仅允许沿第一轴分块")
            if chunk.data.shape != (stop - start, *shape[1:]):
                raise ChunkMappingMismatchError("chunk 数据 shape 与选择范围不一致")
            offsets[chunk.tensor_id] = stop
        for tensor_id, tensor in expected.items():
            if tensor.data.shape:
                complete = offsets[tensor_id] == tensor.data.shape[0]
            else:
                complete = tensor_id in seen_scalars
            if not complete:
                raise ChunkMappingMismatchError(f"输出 chunk 不完整：{tensor_id}")
        outputs = self._outputs
        self._outputs = None
        return outputs


def _spec(
    node: GraphNode, template: OperatorSpec | None = None,
) -> OperatorSpec:
    if template is not None:
        return template.model_copy(update={
            "name": node.operator_name, "version": node.operator_version,
        })
    return OperatorSpec(
        name=node.operator_name, version=node.operator_version,
        category=node.node_type,
        deterministic="bit_exact", stateful=False,
        chunkable=node.node_type not in {"source", "artifact"},
        cacheable=node.node_type not in {"source", "decode", "artifact"},
        supported_dtypes=("uint8", "float32", "float64"),
        config_schema_id=f"pixelprobe.node.{node.node_type}.v1",
    )


class OperatorRegistry:
    def __init__(self) -> None:
        self._planning_by_name: dict[
            str, tuple[OperatorSpec, ConfigValidator, InferOutput]
        ] = {}
        self._runtime_by_name: dict[str, RuntimeHandler] = {}
        self._runtime_by_type: dict[str, RuntimeHandler] = {}

    def register(
        self,
        name: str,
        *,
        spec: OperatorSpec,
        validator: ConfigValidator,
        infer: InferOutput,
        runtime: RuntimeHandler | None = None,
        replace: bool = False,
    ) -> None:
        if not replace and name in self._planning_by_name:
            raise ValueError(f"Operator 已注册：{name}")
        self._planning_by_name[name] = (spec, validator, infer)
        if runtime is not None:
            self.register_runtime(name=name, handler=runtime, replace=replace)

    def register_runtime(
        self,
        *,
        handler: RuntimeHandler,
        name: str | None = None,
        node_type: str | None = None,
        replace: bool = False,
    ) -> None:
        if (name is None) == (node_type is None):
            raise ValueError("name 与 node_type 必须且只能提供一个")
        registry = self._runtime_by_name if name is not None else self._runtime_by_type
        key = name if name is not None else node_type
        assert key is not None
        if not replace and key in registry:
            return
        registry[key] = handler

    def resolve_runtime(self, node: GraphNode) -> RuntimeHandler:
        handler = self._runtime_by_name.get(node.operator_name)
        if handler is None:
            handler = self._runtime_by_type.get(node.node_type)
        if handler is None:
            raise OperatorNotRegisteredError(
                f"Operator 没有注册运行时：{node.operator_name}"
            )
        return handler

    def resolve(self, node: GraphNode) -> PlanningOperator:
        name = node.operator_name
        custom = self._planning_by_name.get(name)
        if custom is not None:
            template, validator, infer = custom
            return PlanningOperator(_spec(node, template), validator, infer)
        if node.node_type == "source":
            return PlanningOperator(_spec(node), MediaSource.model_validate, _infer_none)
        if node.node_type == "decode":
            return PlanningOperator(_spec(node), DecodeConfig.model_validate, _infer_decode)
        if node.node_type == "sample":
            return PlanningOperator(
                _spec(node, SAMPLING_OPERATOR_SPEC),
                SampleNodeConfig.model_validate, _infer_sample,
            )
        if name in {"feature.flow", "feature.farneback"}:
            return PlanningOperator(
                _spec(node, FLOW_OPERATOR_SPEC),
                _validate_flow, _infer_flow,
            )
        if name in {"feature.diff", "feature.frame_difference"}:
            return PlanningOperator(
                _spec(node, FRAME_DIFFERENCE_OPERATOR_SPEC),
                _validate_frame_difference, _infer_frame_difference,
            )
        if name in {"feature.grayscale", "feature.hsv", "feature.lab"}:
            return PlanningOperator(
                _spec(node, COLOR_CONVERSION_OPERATOR_SPEC),
                _validate_color_conversion, _infer_color_conversion,
            )
        if node.node_type == "reduce":
            return PlanningOperator(
                _spec(node, REDUCTION_OPERATOR_SPEC),
                ReductionRequest.model_validate, _infer_reduce,
            )
        if node.node_type == "frequency":
            return PlanningOperator(
                _spec(node, FREQUENCY_OPERATOR_SPEC),
                _validate_frequency, _infer_frequency,
            )
        if node.node_type == "preview":
            return PlanningOperator(
                _spec(node, PREVIEW_OPERATOR_SPEC),
                PreviewConfig.model_validate, _infer_preview,
            )
        if node.node_type == "artifact":
            return PlanningOperator(
                _spec(node), ArtifactConfig.model_validate, _infer_passthrough,
            )
        raise OperatorNotRegisteredError(f"没有注册 Operator：{name}")
