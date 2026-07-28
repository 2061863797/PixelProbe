"""与 Bundle 隔离的本机内容寻址数组缓存。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from pixelprobe.artifacts.array_io import NpyArrayHandle, save_array_handle_npy
from pixelprobe.artifacts.bundle import sha256_file
from pixelprobe.domain.accuracy import AccuracyInfo
from pixelprobe.domain.axes import AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import CoordinateSpace
from pixelprobe.domain.references import ProvenanceRef
from pixelprobe.domain.tensor import ArrayHandle, MemoryArrayHandle, TensorField
from pixelprobe.engine.errors import CacheEntryCorruptError


def _metadata_json_fallback(value: object) -> object:
    """将运行期 NumPy 标量/索引数组转换为可恢复的 JSON 值。

    ``TensorField.attributes['_index_values']`` 只在执行期保存坐标、PTS 等
    数值索引。缓存命中后以列表形式恢复，Bundle Writer 会再次将其规范化为
    NumPy 数组；拒绝 object dtype，避免把不可验证的 Python 对象写入缓存。
    """
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise TypeError("缓存元数据不支持 object dtype 数组")
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"缓存元数据包含不可序列化类型：{type(value).__name__}")


class CacheKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    input_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator_name: str
    operator_version: str
    canonical_config: dict[str, object]
    input_tensor_descriptors: tuple[dict[str, object], ...]
    backend: str = "cpu"
    dtype: str
    precision: str
    execution_semantics_version: str
    artifact_role: str

    def key(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class CacheMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "0.1.0"
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    array_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dtype: str
    shape: tuple[int, ...]
    tensor: "CachedTensorMetadata | None" = None


class CachedTensorMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tensor_id: str
    axes: tuple[AxisSpec, ...]
    channels: tuple[ChannelSpec, ...]
    coordinate_space: CoordinateSpace | None
    axis_mappings: tuple[AxisMapping, ...]
    accuracy: AccuracyInfo
    provenance: ProvenanceRef
    attributes: dict[str, object]


@dataclass(slots=True, frozen=True)
class CacheLookup:
    handle: NpyArrayHandle | None
    warning: CacheEntryCorruptError | None = None


class LocalArrayCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.entries = self.root / "entries"
        self.quarantine = self.root / "quarantine"
        self.partials = self.root / "partials"
        self.entries.mkdir(parents=True, exist_ok=True)
        self.quarantine.mkdir(parents=True, exist_ok=True)
        self.partials.mkdir(parents=True, exist_ok=True)
        self._warnings: list[CacheEntryCorruptError] = []

    def put(self, key_input: CacheKeyInput, array: np.ndarray) -> NpyArrayHandle:
        return self._put(key_input, MemoryArrayHandle(array), None)

    def put_tensor(
        self, key_input: CacheKeyInput, tensor: TensorField,
    ) -> TensorField:
        if tensor.validity is not None:
            raise ValueError("1.0 缓存暂不接受独立 validity 数组")
        metadata = CachedTensorMetadata(
            tensor_id=tensor.tensor_id,
            axes=tensor.axes,
            channels=tensor.channels,
            coordinate_space=tensor.coordinate_space,
            axis_mappings=tensor.axis_mappings,
            accuracy=tensor.accuracy,
            provenance=tensor.provenance,
            attributes=tensor.attributes,
        )
        handle = self._put(key_input, tensor.data, metadata)
        return self._tensor(metadata, handle)

    def _put(
        self,
        key_input: CacheKeyInput,
        source: ArrayHandle,
        tensor: CachedTensorMetadata | None,
    ) -> NpyArrayHandle:
        key = key_input.key()
        target = self.entries / key
        if target.exists():
            lookup = self.get(key_input)
            if lookup.handle is not None and (
                tensor is None or self._metadata(target).tensor is not None
            ):
                return lookup.handle
            if lookup.handle is not None:
                lookup.handle.close()
            quarantine = self.quarantine / f"{key}-{uuid.uuid4().hex}"
            os.replace(target, quarantine)
        temporary = self.entries / f".{key}.tmp-{uuid.uuid4().hex}"
        try:
            temporary.mkdir()
            handle = save_array_handle_npy(source, temporary / "data.npy")
            handle.close()
            metadata = CacheMetadata(
                key=key,
                array_sha256=sha256_file(temporary / "data.npy"),
                dtype=source.dtype,
                shape=source.shape,
                tensor=tensor,
            )
            with (temporary / "metadata.json").open("xb") as output:
                output.write(
                    metadata.model_dump_json(
                        indent=2, fallback=_metadata_json_fallback,
                    ).encode("utf-8") + b"\n"
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        return NpyArrayHandle(target / "data.npy")

    @staticmethod
    def _metadata(target: Path) -> CacheMetadata:
        return CacheMetadata.model_validate_json((target / "metadata.json").read_bytes())

    def get(self, key_input: CacheKeyInput) -> CacheLookup:
        key = key_input.key()
        target = self.entries / key
        if not target.is_dir():
            return CacheLookup(None)
        try:
            metadata = self._metadata(target)
            data_path = target / "data.npy"
            if metadata.key != key or sha256_file(data_path) != metadata.array_sha256:
                raise ValueError("缓存校验和不匹配")
            handle = NpyArrayHandle(data_path)
            if handle.dtype != metadata.dtype or handle.shape != metadata.shape:
                handle.close()
                raise ValueError("缓存 shape/dtype 不匹配")
            return CacheLookup(handle)
        except Exception as exc:
            quarantine = self.quarantine / f"{key}-{uuid.uuid4().hex}"
            try:
                os.replace(target, quarantine)
            except OSError:
                pass
            warning = CacheEntryCorruptError(
                f"缓存已损坏并隔离：{key}（{exc}）"
            )
            self._warnings.append(warning)
            return CacheLookup(None, warning)

    def pop_warnings(self) -> tuple[CacheEntryCorruptError, ...]:
        warnings = tuple(self._warnings)
        self._warnings.clear()
        return warnings

    def get_tensor(self, key_input: CacheKeyInput) -> TensorField | None:
        lookup = self.get(key_input)
        if lookup.handle is None:
            return None
        metadata = self._metadata(self.entries / key_input.key()).tensor
        if metadata is None:
            lookup.handle.close()
            return None
        return self._tensor(metadata, lookup.handle)

    @staticmethod
    def _tensor(
        metadata: CachedTensorMetadata, handle: NpyArrayHandle,
    ) -> TensorField:
        return TensorField(
            tensor_id=metadata.tensor_id,
            data=handle,
            axes=metadata.axes,
            channels=metadata.channels,
            coordinate_space=metadata.coordinate_space,
            axis_mappings=metadata.axis_mappings,
            validity=None,
            accuracy=metadata.accuracy,
            provenance=metadata.provenance,
            attributes=metadata.attributes,
        )

    def clear(self) -> int:
        count = sum(1 for item in self.entries.iterdir() if item.is_dir())
        for item in tuple(self.entries.iterdir()):
            if item.is_dir():
                shutil.rmtree(item)
        for item in tuple(self.partials.iterdir()):
            if item.is_dir():
                shutil.rmtree(item)
        return count
