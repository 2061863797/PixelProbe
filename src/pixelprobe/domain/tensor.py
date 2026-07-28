"""TensorField、数组句柄及其公开描述模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.domain.accuracy import AccuracyInfo
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace
from pixelprobe.domain.errors import (
    ArraySelectionOutOfRangeError,
    AxisShapeMismatchError,
    ChannelCountMismatchError,
    MaterializationLimitExceededError,
    SchemaVersionUnsupportedError,
)
from pixelprobe.domain.references import ArtifactRef, ProvenanceRef


class StorageKind(StrEnum):
    MEMORY = "memory"
    NPY = "npy"
    MEMMAP = "memmap"
    ZARR = "zarr"
    ARTIFACT = "artifact"


@runtime_checkable
class ArrayHandle(Protocol):
    @property
    def shape(self) -> tuple[int, ...]: ...

    @property
    def dtype(self) -> str: ...

    @property
    def storage_kind(self) -> StorageKind: ...

    @property
    def chunk_shape(self) -> tuple[int, ...] | None: ...

    def read(self, selection: tuple[slice | int, ...]) -> np.ndarray: ...

    def materialize(self, *, max_bytes: int | None = None) -> np.ndarray: ...


class MemoryArrayHandle:
    """持有完整精确数组的只读内存句柄；读取始终返回副本。"""

    __slots__ = ("_array",)

    def __init__(self, array: np.ndarray) -> None:
        if not isinstance(array, np.ndarray):
            raise TypeError("array 必须是 NumPy 数组")
        self._array = np.array(array, copy=True, order="C")
        self._array.setflags(write=False)

    @property
    def shape(self) -> tuple[int, ...]:
        return self._array.shape

    @property
    def dtype(self) -> str:
        return str(self._array.dtype)

    @property
    def storage_kind(self) -> StorageKind:
        return StorageKind.MEMORY

    @property
    def chunk_shape(self) -> tuple[int, ...] | None:
        return None

    def read(self, selection: tuple[slice | int, ...]) -> np.ndarray:
        if len(selection) != self._array.ndim:
            raise ArraySelectionOutOfRangeError(
                f"selection 维数 {len(selection)} 与数组维数 {self._array.ndim} 不一致"
            )
        for axis, (item, length) in enumerate(zip(selection, self._array.shape)):
            if isinstance(item, int):
                if item < 0 or item >= length:
                    raise ArraySelectionOutOfRangeError(
                        f"第 {axis} 轴索引 {item} 超出 [0,{length})"
                    )
                continue
            if not isinstance(item, slice):
                raise TypeError("selection 只支持 int 与 slice")
            start = 0 if item.start is None else item.start
            stop = length if item.stop is None else item.stop
            step = 1 if item.step is None else item.step
            if start < 0 or stop < 0 or start > length or stop > length or step <= 0:
                raise ArraySelectionOutOfRangeError(
                    f"第 {axis} 轴切片必须位于 [0,{length}] 且 step > 0"
                )
        return np.array(self._array[selection], copy=True)

    def materialize(self, *, max_bytes: int | None = None) -> np.ndarray:
        if max_bytes is not None and self._array.nbytes > max_bytes:
            raise MaterializationLimitExceededError(
                f"数组需要 {self._array.nbytes} 字节，超过限制 {max_bytes} 字节"
            )
        return np.array(self._array, copy=True)


@dataclass(slots=True, frozen=True)
class TensorField:
    tensor_id: str
    data: ArrayHandle
    axes: tuple[AxisSpec, ...]
    channels: tuple[ChannelSpec, ...]
    coordinate_space: CoordinateSpace | None
    axis_mappings: tuple[AxisMapping, ...]
    validity: ArrayHandle | None
    accuracy: AccuracyInfo
    provenance: ProvenanceRef
    attributes: dict[str, object]

    def __post_init__(self) -> None:
        if not self.tensor_id:
            raise ValueError("tensor_id 不能为空")
        if len(self.axes) != len(self.data.shape):
            raise AxisShapeMismatchError("轴数量与数组维数不一致", object_id=self.tensor_id)
        if len({axis.name for axis in self.axes}) != len(self.axes):
            raise AxisShapeMismatchError("轴名称不能重复", object_id=self.tensor_id)
        for axis, length in zip(self.axes, self.data.shape):
            if axis.length != length:
                raise AxisShapeMismatchError(
                    f"轴 {axis.name} 长度 {axis.length} 与数组维度 {length} 不一致",
                    object_id=self.tensor_id,
                )
        channel_axes = [axis for axis in self.axes if axis.kind == AxisKind.CHANNEL]
        if channel_axes:
            if len(channel_axes) != 1 or channel_axes[0].length != len(self.channels):
                raise ChannelCountMismatchError("channel 轴与 channels 数量不一致", object_id=self.tensor_id)
        elif self.channels:
            raise ChannelCountMismatchError("存在 channels 但缺少 channel 轴", object_id=self.tensor_id)
        if self.validity is not None:
            if np.dtype(self.validity.dtype) != np.dtype(bool):
                raise ValueError("validity dtype 必须是 bool")
            try:
                np.broadcast_shapes(self.validity.shape, self.data.shape)
            except ValueError as exc:
                raise AxisShapeMismatchError("validity 不能广播到 data shape", object_id=self.tensor_id) from exc
        object.__setattr__(self, "attributes", dict(self.attributes))


class TensorFieldDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    tensor_id: str = Field(min_length=1)
    data_ref: ArtifactRef
    dtype: str = Field(min_length=1)
    shape: tuple[int, ...]
    axes: tuple[AxisSpec, ...]
    channels: tuple[ChannelSpec, ...]
    coordinate_space_id: str | None
    mapping_ids: tuple[str, ...]
    validity_ref: ArtifactRef | None = None
    accuracy: AccuracyInfo
    provenance_id: str = Field(min_length=1)
    attributes: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> "TensorFieldDescriptor":
        if self.schema_version.split(".", 1)[0] != "0":
            raise SchemaVersionUnsupportedError(
                f"不支持 TensorField Schema {self.schema_version}"
            )
        if any(length < 0 for length in self.shape):
            raise ValueError("shape 不能包含负数")
        if len(self.axes) != len(self.shape):
            raise ValueError("axes 数量必须等于 shape 维数")
        if any(axis.length != length for axis, length in zip(self.axes, self.shape)):
            raise ValueError("AxisSpec.length 必须与 shape 对应维度一致")
        channel_axes = [axis for axis in self.axes if axis.kind == AxisKind.CHANNEL]
        if bool(channel_axes) != bool(self.channels):
            raise ValueError("channel 轴与 channels 必须同时存在或同时省略")
        if channel_axes and (len(channel_axes) != 1 or channel_axes[0].length != len(self.channels)):
            raise ValueError("channel 轴长度必须等于 channels 数量")
        try:
            np.dtype(self.dtype)
        except TypeError as exc:
            raise ValueError(f"无效 dtype：{self.dtype}") from exc
        return self
