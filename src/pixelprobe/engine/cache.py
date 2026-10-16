"""与 Bundle 隔离的本机内容寻址数组缓存。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import time
import re
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
from pixelprobe.engine.cache_lifecycle import CacheInUseError, gate, reference, require_idle
from pixelprobe.utils.file_lock import require_plain


class CachedArrayHandle(NpyArrayHandle):
    """即使生成已结束，仍持有文件的数组句柄也保护缓存。"""
    def __init__(self, path: Path, root: Path):
        self._cache_reference = reference(root)
        try:
            super().__init__(path)
        except BaseException:
            self._cache_reference.close()
            raise

    def close(self):
        try:
            super().close()
        finally:
            self._cache_reference.close()


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
        require_plain(self.root)
        self.root = self.root.resolve()
        self.entries = self.root / "entries"
        self.quarantine = self.root / "quarantine"
        self.partials = self.root / "partials"
        for directory in (self.entries, self.quarantine, self.partials):
            require_plain(directory)
            directory.mkdir(parents=True, exist_ok=True)
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
        self, key_input: CacheKeyInput, source: ArrayHandle,
        tensor: CachedTensorMetadata | None,
    ) -> NpyArrayHandle:
        with self.reference():
            return self._put_impl(key_input, source, tensor)

    def reference(self):
        return reference(self.root)

    def _put_impl(
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
            from pixelprobe.utils.resource_budget import moved
            moved(target, quarantine)
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
            from pixelprobe.utils.resource_budget import write_bytes, moved, removed
            write_bytes(temporary / "metadata.json", metadata.model_dump_json(
                indent=2, fallback=_metadata_json_fallback,
            ).encode("utf-8") + b"\n")
            os.replace(temporary, target)
            moved(temporary, target)
        finally:
            from pixelprobe.utils.resource_budget import removed
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            removed(temporary)
        return CachedArrayHandle(target / "data.npy", self.root)

    @staticmethod
    def _metadata(target: Path) -> CacheMetadata:
        return CacheMetadata.model_validate_json((target / "metadata.json").read_bytes())

    def get(self, key_input: CacheKeyInput) -> CacheLookup:
        with self.reference():
            return self._get_impl(key_input)

    def _get_impl(self, key_input: CacheKeyInput) -> CacheLookup:
        key = key_input.key()
        target = self.entries / key
        if not target.is_dir():
            return CacheLookup(None)
        try:
            from pixelprobe.utils.resource_budget import reserve_write
            for filename in ("metadata.json", "data.npy"):
                existing = target / filename
                reserve_write(existing, existing.stat().st_size)
            metadata = self._metadata(target)
            data_path = target / "data.npy"
            if metadata.key != key or sha256_file(data_path) != metadata.array_sha256:
                raise ValueError("缓存校验和不匹配")
            handle = CachedArrayHandle(data_path, self.root)
            if handle.dtype != metadata.dtype or handle.shape != metadata.shape:
                handle.close()
                raise ValueError("缓存 shape/dtype 不匹配")
            return CacheLookup(handle)
        except CacheInUseError:
            raise
        except Exception as exc:
            from pixelprobe.engine.errors import EngineError
            from pixelprobe.domain.errors import MaterializationLimitExceededError
            if isinstance(exc, (EngineError, MaterializationLimitExceededError)):
                raise
            quarantine = self.quarantine / f"{key}-{uuid.uuid4().hex}"
            try:
                os.replace(target, quarantine)
                from pixelprobe.utils.resource_budget import moved
                moved(target, quarantine)
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
        # MCP checkpoint 缓存必须经任务引用检查，不能从 CLI 绕过维护接口。
        root = self.root.resolve()
        if any(parent.name == ".checkpoints" for parent in root.parents):
            from pixelprobe.models.errors import InvalidRangeError
            raise InvalidRangeError("MCP checkpoint 缓存请通过 pixelprobe_system action=storage 清理")
        return self.maintain(dry_run=False)["removed_entries"]

    def maintain(self, *, dry_run: bool = True, max_age_seconds: float = 0) -> dict:
        """仅清理有可验证归属的条目和显式标记的 partial，未知内容保留。"""
        if not np.isfinite(max_age_seconds) or max_age_seconds < 0:
            raise ValueError("缓存保留时间必须是非负有限数")
        if any(parent.name == ".checkpoints" for parent in self.root.parents):
            from pixelprobe.models.errors import InvalidRangeError
            raise InvalidRangeError("MCP checkpoint 缓存请通过 pixelprobe_system action=storage 清理")
        report = {"dry_run": dry_run, "entries": 0, "bytes": 0, "eligible_entries": 0,
                  "reclaimable_bytes": 0, "removed_entries": 0, "skipped": 0}
        with gate(self.root):
            require_idle(self.root)
            scanned = 0
            for directory in (self.entries, self.quarantine, self.partials):
                require_plain(directory)
                for item in directory.iterdir():
                    scanned += 1
                    if scanned > 10000:
                        report["skipped"] += 1
                        return report
                    try:
                        require_plain(item)
                        if not item.is_dir():
                            raise ValueError("未知文件")
                        if directory == self.partials:
                            marker = item / ".pixelprobe-owned.json"
                            require_plain(marker)
                            if json.loads(marker.read_text("utf-8")) != {
                                "owner": "pixelprobe", "kind": "partial", "request_sha256": item.name,
                            } or re.fullmatch(r"[0-9a-f]{64}", item.name) is None:
                                raise ValueError("partial 归属未知")
                        else:
                            require_plain(item / "metadata.json")
                            metadata = self._metadata(item)
                            expected = metadata.key if directory == self.entries else metadata.key + "-"
                            if ((directory == self.entries and item.name != expected)
                                    or (directory == self.quarantine and not item.name.startswith(expected))):
                                raise ValueError("缓存归属不匹配")
                            if {child.name for child in item.iterdir()} != {"data.npy", "metadata.json"}:
                                raise ValueError("缓存包含未知内容")
                        size, modified, pending = 0, item.stat().st_mtime, [item]
                        count = 0
                        while pending:
                            path = pending.pop()
                            require_plain(path)
                            count += 1
                            if count > 10000:
                                raise ValueError("缓存目录过大")
                            modified = max(modified, path.stat().st_mtime)
                            if path.is_dir():
                                for child in path.iterdir():
                                    if count + len(pending) >= 10000:
                                        raise ValueError("缓存目录过大")
                                    pending.append(child)
                            elif path.is_file():
                                size += path.stat().st_size
                            else:
                                raise ValueError("未知文件类型")
                        report["entries"] += 1
                        report["bytes"] += size
                        if modified > time.time() - max_age_seconds:
                            continue
                        report["eligible_entries"] += 1
                        report["reclaimable_bytes"] += size
                        if not dry_run:
                            if item.resolve().parent != directory.resolve():
                                raise ValueError("缓存路径变化")
                            shutil.rmtree(item)
                            report["removed_entries"] += 1
                    except (OSError, ValueError):
                        report["skipped"] += 1
        return report
