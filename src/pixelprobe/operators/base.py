"""V0.7 Operator 契约与直接执行所需类型。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Literal, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.domain.axes import AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.tensor import TensorField


class HaloSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    before: int = Field(default=0, ge=0)
    after: int = Field(default=0, ge=0)


class OperatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    category: Literal[
        "source", "decode", "transform", "sample",
        "reduce", "frequency", "preview", "artifact",
    ]
    deterministic: Literal["bit_exact", "tolerance", "nondeterministic"]
    stateful: bool
    chunkable: bool
    cacheable: bool
    temporal_halo: HaloSpec = Field(default_factory=HaloSpec)
    spatial_halo_x: HaloSpec = Field(default_factory=HaloSpec)
    spatial_halo_y: HaloSpec = Field(default_factory=HaloSpec)
    supported_devices: tuple[Literal["cpu", "cuda"], ...] = ("cpu",)
    supported_dtypes: tuple[str, ...]
    config_schema_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_runtime_support(self) -> "OperatorSpec":
        if "cpu" not in self.supported_devices:
            raise ValueError("1.0 前的 Operator 必须支持 cpu")
        if not self.supported_dtypes:
            raise ValueError("supported_dtypes 不能为空")
        if self.deterministic == "nondeterministic" and self.cacheable:
            raise ValueError("非确定性 Operator 默认不能缓存")
        return self


class TensorDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dtype: str
    shape: tuple[int | None, ...]
    axes: tuple[AxisSpec, ...]
    channels: tuple[ChannelSpec, ...]
    coordinate_space_id: str | None

    @model_validator(mode="after")
    def validate_axes(self) -> "TensorDescriptor":
        if len(self.axes) != len(self.shape):
            raise ValueError("axes 数量必须等于 shape 维数")
        for axis, length in zip(self.axes, self.shape):
            if length is not None and axis.length != length:
                raise ValueError(f"轴 {axis.name} 长度与 shape 不一致")
        np.dtype(self.dtype)
        return self


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_memory_bytes: int = Field(gt=0)
    max_temporary_bytes: int | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)
    preferred_chunk_bytes: int = Field(default=67_108_864, gt=0)
    allow_partial: bool = False


class OperatorPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operator_name: str
    operator_version: str
    backend: Literal["cpu"] = "cpu"
    input_descriptors: tuple[TensorDescriptor, ...]
    output_descriptors: tuple[TensorDescriptor, ...]
    chunk_axes: tuple[str, ...] = ()
    chunk_shape: tuple[int, ...] | None = None
    estimated_peak_memory_bytes: int = Field(ge=0)
    estimated_temporary_bytes: int = Field(ge=0)
    execution_order: Literal["sequential", "parallel_chunks"] = "sequential"
    cache_key: str | None = None


@dataclass(slots=True, frozen=True)
class TensorChunk:
    tensor_id: str
    data: np.ndarray
    read_selection: tuple[slice, ...]
    core_selection: tuple[slice, ...]
    axis_mappings: tuple[AxisMapping, ...]
    validity: np.ndarray | None
    chunk_index: tuple[int, ...]


@dataclass(slots=True, frozen=True)
class RuntimeInvocation:
    """Executor 交给已注册运行时处理器的单节点调用。"""

    node_id: str
    node_type: str
    operator_name: str
    operator_version: str
    inputs: tuple[object, ...]
    input_chunks: tuple[Iterator[TensorChunk], ...]
    config: dict[str, object]
    context: "ExecutionContext"
    file_backed: bool
    partial_path: Path | None
    completed_positions: frozenset[int]
    completed_hashes: Mapping[int, str]
    on_chunk_completed: Callable[[int, str], None] | None


class ExecutionContext(Protocol):
    execution_id: str
    resources: ResourcePolicy

    def report_progress(
        self, node_id: str, completed: int, total: int | None,
    ) -> None: ...


class Operator(ABC):
    """生命周期固定的内部 Operator 基类。"""

    spec: OperatorSpec

    @abstractmethod
    def validate_config(self, config: Mapping[str, object]) -> BaseModel: ...

    @abstractmethod
    def infer_output(
        self,
        inputs: tuple[TensorDescriptor, ...],
        config: BaseModel,
    ) -> tuple[TensorDescriptor, ...]: ...

    @abstractmethod
    def plan(
        self,
        inputs: tuple[TensorDescriptor, ...],
        outputs: tuple[TensorDescriptor, ...],
        config: BaseModel,
        resources: ResourcePolicy,
    ) -> OperatorPlan: ...

    @abstractmethod
    def execute(
        self,
        context: ExecutionContext,
        inputs: tuple[Iterator[TensorChunk], ...],
        config: BaseModel,
        plan: OperatorPlan,
    ) -> Iterator[TensorChunk]: ...

    @abstractmethod
    def finalize(
        self,
        context: ExecutionContext,
        chunks: Iterator[TensorChunk],
        plan: OperatorPlan,
    ) -> tuple[TensorField, ...]: ...
