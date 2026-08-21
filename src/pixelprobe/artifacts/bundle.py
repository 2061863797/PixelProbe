"""可移动、可校验且事务提交的 PixelProbe Bundle。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import secrets
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from collections.abc import Mapping

import numpy as np
import pydantic
from PIL import Image
from pydantic import ValidationError

from pixelprobe.artifacts.array_io import NpyArrayHandle, save_array_handle_npy
from pixelprobe.artifacts.zarr_io import ZarrArrayHandle, save_array_handle_zarr
from pixelprobe.artifacts.errors import (
    ArtifactChecksumMismatchError,
    ArtifactFileMissingError,
    ArtifactSchemaMismatchError,
    BundleManifestInvalidError,
    BundleManifestMissingError,
    BundleIncompleteError,
    BundlePathUnsafeError,
    BundleSchemaUnsupportedError,
    BundleTargetExistsError,
    BundleWriteError,
    ProvenanceGraphInvalidError,
)
from pixelprobe.artifacts.models import (
    ArtifactRecord,
    BundleManifest,
    MappingCollection,
    ProducerInfo,
    ProvenanceGraph,
    ProvenanceNode,
    SourceRecord,
    StorageDescriptor,
    StoredFile,
    TensorSchemaRecord,
)
from pixelprobe.domain.references import ArtifactRef, ProvenanceRef
from pixelprobe.domain.tensor import MemoryArrayHandle, TensorField, TensorFieldDescriptor
from pixelprobe.engine.request import RepresentationRequest
from pixelprobe.version import __version__

BUNDLE_SCHEMA_VERSION = "0.1.0"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 1_000_000
_TOP_LEVEL = {
    "data": "artifacts", "preview": "previews", "index": "indexes",
    "metadata": "metadata", "log": "logs",
}


def _uuid7() -> str:
    milliseconds = int(time.time() * 1000) & ((1 << 48) - 1)
    random_bits = secrets.randbits(74)
    value = milliseconds << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0x2 << 62
    value |= random_bits & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_bounded_json(path: Path, *, max_bytes: int = _MAX_METADATA_BYTES) -> object:
    if path.stat().st_size > max_bytes:
        raise ValueError(f"JSON 文件超过 {max_bytes} 字节限制：{path.name}")
    value = json.loads(path.read_bytes())
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"JSON 嵌套深度超过 {_MAX_JSON_DEPTH}")
        if nodes > _MAX_JSON_NODES:
            raise ValueError(f"JSON 节点数超过 {_MAX_JSON_NODES}")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _remove_or_abandon(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        abandoned = path.parent / f".abandoned-{uuid.uuid4().hex}"
        try:
            os.replace(path, abandoned)
        except OSError as rename_exc:
            raise BundleWriteError(
                f"临时 Bundle 清理失败且无法隔离：{path}（{rename_exc}）"
            ) from exc
        raise BundleWriteError(
            f"临时 Bundle 清理失败，已隔离为：{abandoned}"
        ) from exc


def _stored(root: Path, path: Path, media_type: str) -> StoredFile:
    return StoredFile(
        uri=path.relative_to(root).as_posix(),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
        media_type=media_type,
    )


def _stored_tree(root: Path, directory: Path) -> tuple[StoredFile, ...]:
    return tuple(
        _stored(root, path, "application/octet-stream")
        for path in sorted(item for item in directory.rglob("*") if item.is_file())
    )


def _merkle_sha256(files: tuple[StoredFile, ...]) -> str:
    digest = hashlib.sha256()
    for stored in sorted(files, key=lambda item: item.uri.encode("utf-8")):
        digest.update(stored.uri.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(stored.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _replace_reference_ids(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace_reference_ids(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_reference_ids(item, replacements) for item in value)
    if isinstance(value, dict):
        return {
            key: _replace_reference_ids(item, replacements)
            for key, item in value.items()
        }
    return value


def _source_metadata_policies(
    requests: tuple[RepresentationRequest, ...],
    sources: tuple[SourceRecord, ...],
) -> dict[str, str]:
    """合并来源隐私策略；只有全体明确 full 时才允许保留路径。"""
    policies: dict[str, list[str]] = {}
    for source in sources:
        policies.setdefault(source.source_id, []).append(source.metadata_policy)
    for request in requests:
        policies.setdefault(request.source.source_id, []).append(
            request.output.metadata_policy
        )
    return {
        source_id: "full" if values and all(value == "full" for value in values)
        else "safe"
        for source_id, values in policies.items()
    }


def _register_path_replacement(
    replacements: dict[str, str], value: str | None, replacement: str,
) -> None:
    """同时登记原始文字和规范化绝对路径，覆盖 DAG 中的两种表示。"""
    if not value or value.startswith("source://"):
        return
    replacements[value] = replacement
    try:
        replacements[str(Path(value).expanduser().resolve(strict=False))] = replacement
    except (OSError, ValueError):
        # 非文件 URI 或平台不接受的路径由原始文字匹配处理。
        pass


def _source_path_replacements(
    requests: tuple[RepresentationRequest, ...],
    sources: tuple[SourceRecord, ...],
    policies: Mapping[str, str],
) -> dict[str, str]:
    """为非 full 来源建立媒体路径到稳定 source URI 的替换表。"""
    replacements: dict[str, str] = {}
    for request in requests:
        source_id = request.source.source_id
        if policies.get(source_id, "safe") == "full":
            continue
        _register_path_replacement(
            replacements, request.source.uri, f"source://{source_id}",
        )
        _register_path_replacement(
            replacements,
            request.source.sequence_manifest,
            f"source://{source_id}/manifest",
        )
    for source in sources:
        if policies.get(source.source_id, "safe") != "full":
            _register_path_replacement(
                replacements, source.original_uri, f"source://{source.source_id}",
            )
    return replacements


def _redact_source_paths(value: object, replacements: Mapping[str, str]) -> object:
    """深度脱敏 metadata；同时处理嵌入的规范 JSON（例如 config_json）。"""
    if isinstance(value, str):
        direct = replacements.get(value)
        if direct is not None:
            return direct
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value
        # 只有对象/数组是嵌入配置；避免把普通 JSON 标量的文本表示改写掉。
        if not isinstance(parsed, (dict, list)):
            return value
        redacted = _redact_source_paths(parsed, replacements)
        if redacted == parsed:
            return value
        return json.dumps(
            redacted, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"), sort_keys=True,
        )
    if isinstance(value, Mapping):
        return {
            key: _redact_source_paths(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_source_paths(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_source_paths(item, replacements) for item in value)
    return value


def _mapping_reference_ids(parameters: dict[str, object]) -> set[str]:
    references: set[str] = set()
    for key, value in parameters.items():
        if key in {"coordinates_ref", "starts_ref", "ends_ref", "weights_ref"}:
            if not isinstance(value, str):
                raise ArtifactSchemaMismatchError(
                    f"AxisMapping {key} 必须是 Artifact ID"
                )
            references.add(value)
    return references


def _default_chunk_shape(shape: tuple[int, ...], dtype: str) -> tuple[int, ...]:
    chunk = list(shape)
    itemsize = np.dtype(dtype).itemsize
    while int(np.prod(chunk, dtype=np.int64)) * itemsize > 16 * 1024 * 1024:
        axis = max(range(len(chunk)), key=chunk.__getitem__)
        chunk[axis] = max(1, (chunk[axis] + 1) // 2)
    return tuple(chunk)


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _producer() -> ProducerInfo:
    return ProducerInfo(
        version=__version__,
        python_version=platform.python_version(),
        platform=f"{sys.platform}-{platform.machine().lower()}",
        dependencies={
            "numpy": np.__version__,
            "pydantic": pydantic.__version__,
            "pyav": _package_version("av"),
        },
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _resolve_stored_path(root: Path, uri: str, expected_top: str) -> Path:
    if "\\" in uri or "\x00" in uri:
        raise BundlePathUnsafeError(f"Bundle URI 非法：{uri}")
    pure = PurePosixPath(uri)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise BundlePathUnsafeError(f"Bundle URI 非法：{uri}")
    if pure.parts[0] != expected_top or ":" in pure.parts[0]:
        raise BundlePathUnsafeError(f"Bundle URI 顶层目录错误：{uri}")
    candidate = root.joinpath(*pure.parts)
    for parent in (root, *candidate.parents):
        if parent == root.parent:
            break
        if parent.exists() and (parent.is_symlink() or _is_reparse_point(parent)):
            raise BundlePathUnsafeError(f"Bundle 路径包含链接或重解析点：{uri}")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BundlePathUnsafeError(f"Bundle URI 越界：{uri}") from exc
    return candidate


def _unknown_field_notices(value: object, path: str = "manifest") -> list[str]:
    notices: list[str] = []
    if isinstance(value, pydantic.BaseModel):
        for name in sorted((value.model_extra or {})):
            notices.append(f"{path}.{name}")
        for name in value.__class__.model_fields:
            child = getattr(value, name, None)
            notices.extend(_unknown_field_notices(child, f"{path}.{name}"))
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            notices.extend(_unknown_field_notices(child, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for name, child in value.items():
            notices.extend(_unknown_field_notices(child, f"{path}.{name}"))
    return notices


def _validate_semantics(root: Path, manifest: BundleManifest) -> None:
    records = {record.artifact_id: record for record in manifest.artifacts}
    if any(not record.complete for record in manifest.artifacts):
        raise BundleIncompleteError("Bundle 包含 complete=false 的 Artifact")

    mappings: dict[str, object] = {}
    if manifest.mappings_artifact_id is not None:
        mapping_record = records[manifest.mappings_artifact_id]
        if mapping_record.kind != "index" or mapping_record.role != "axis_mappings":
            raise ArtifactSchemaMismatchError("mappings_artifact_id 未指向 AxisMapping Index")
        mapping_path = _resolve_stored_path(
            root, mapping_record.storage.files[0].uri, "indexes",
        )
        try:
            collection = MappingCollection.model_validate(
                _load_bounded_json(mapping_path)
            )
        except Exception as exc:
            raise ArtifactSchemaMismatchError("AxisMapping 集合无效") from exc
        mappings = {mapping.mapping_id: mapping for mapping in collection.mappings}

    for record in manifest.artifacts:
        if record.kind not in {"data", "preview"}:
            continue
        if record.tensor_schema_artifact_id is None:
            raise ArtifactSchemaMismatchError(f"{record.artifact_id} 缺少 Tensor Schema")
        schema_record = records[record.tensor_schema_artifact_id]
        if schema_record.kind != "metadata" or schema_record.role != "tensor_schema":
            raise ArtifactSchemaMismatchError("tensor_schema_artifact_id 指向错误类型")
        schema_path = _resolve_stored_path(
            root, schema_record.storage.files[0].uri, "metadata",
        )
        try:
            schema = TensorSchemaRecord.model_validate(
                _load_bounded_json(schema_path)
            )
        except Exception as exc:
            raise ArtifactSchemaMismatchError(f"Tensor Schema 无效：{record.artifact_id}") from exc
        descriptor = schema.descriptor
        if descriptor.data_ref.artifact_id != record.artifact_id:
            raise ArtifactSchemaMismatchError("Tensor data_ref.artifact_id 不匹配")
        if descriptor.shape != record.storage.shape or np.dtype(descriptor.dtype) != np.dtype(record.storage.dtype):
            raise ArtifactSchemaMismatchError("Tensor Schema shape/dtype 与 Artifact 不匹配")
        if descriptor.mapping_ids != record.mapping_ids:
            raise ArtifactSchemaMismatchError("Tensor Schema mapping_ids 与 Artifact 不匹配")
        if descriptor.validity_ref is not None:
            validity_record = records.get(descriptor.validity_ref.artifact_id)
            if (
                validity_record is None
                or validity_record.kind != "index"
                or validity_record.role != "validity_mask"
                or validity_record.storage.format != "npy"
                or len(validity_record.storage.files) != 1
                or validity_record.storage.dtype is None
                or validity_record.storage.shape is None
                or validity_record.attributes.get("source_tensor_id") != descriptor.tensor_id
                or validity_record.attributes.get("source_data_artifact_id")
                != record.artifact_id
            ):
                raise ArtifactSchemaMismatchError("Tensor validity_ref 指向无效 Validity Index")
            validity_file = validity_record.storage.files[0]
            if (
                descriptor.validity_ref.uri != validity_file.uri
                or descriptor.validity_ref.sha256 != validity_file.sha256
                or descriptor.validity_ref.media_type != validity_file.media_type
                or np.dtype(validity_record.storage.dtype) != np.dtype(bool)
            ):
                raise ArtifactSchemaMismatchError("Tensor validity_ref 与 Validity Index 不一致")
            try:
                np.broadcast_shapes(validity_record.storage.shape, record.storage.shape)
            except ValueError as exc:
                raise ArtifactSchemaMismatchError(
                    "Validity Index 不能广播到 Tensor data"
                ) from exc
        if not set(record.mapping_ids) <= set(mappings):
            raise ArtifactSchemaMismatchError("Tensor 引用了缺失的 AxisMapping")
        axis_mapping_ids = {
            axis.mapping_id for axis in descriptor.axes if axis.mapping_id is not None
        }
        if not axis_mapping_ids <= set(record.mapping_ids):
            raise ArtifactSchemaMismatchError("AxisSpec 引用了未登记的 AxisMapping")
        for axis in descriptor.axes:
            reference = axis.coordinates_ref
            if reference is None:
                continue
            if reference.uri.startswith("memory://"):
                raise ArtifactSchemaMismatchError("Bundle 不能包含执行期 memory:// 坐标引用")
            index_record = records.get(reference.artifact_id)
            if index_record is None or index_record.kind != "index":
                raise ArtifactSchemaMismatchError("AxisSpec 坐标引用缺失 IndexArtifact")
            stored = index_record.storage.files[0]
            if (
                reference.uri != stored.uri
                or reference.sha256 != stored.sha256
                or reference.media_type != stored.media_type
            ):
                raise ArtifactSchemaMismatchError("AxisSpec 坐标引用与 IndexArtifact 不一致")
            if not index_record.storage.shape or index_record.storage.shape[0] != axis.length:
                raise ArtifactSchemaMismatchError("坐标 Index 长度与 AxisSpec.length 不一致")
        for mapping_id in record.mapping_ids:
            mapping = mappings[mapping_id]
            for reference_id in _mapping_reference_ids(mapping.parameters):
                index_record = records.get(reference_id)
                if index_record is None or index_record.kind != "index":
                    raise ArtifactSchemaMismatchError(
                        "AxisMapping 引用了缺失的 IndexArtifact"
                    )
        if descriptor.coordinate_space_id != (
            schema.coordinate_space.coordinate_space_id if schema.coordinate_space else None
        ):
            raise ArtifactSchemaMismatchError("Tensor CoordinateSpace 引用不匹配")
        if record.storage.format == "zarr":
            if (
                not record.storage.chunk_shape
                or not record.storage.compression
                or not all(
                    stored.uri.startswith(descriptor.data_ref.uri.rstrip("/") + "/")
                    for stored in record.storage.files
                )
                or descriptor.data_ref.sha256 != _merkle_sha256(record.storage.files)
                or descriptor.data_ref.media_type != "application/vnd.zarr"
            ):
                raise ArtifactSchemaMismatchError(
                    "Zarr data_ref、chunk 或压缩描述不完整"
                )
        else:
            stored = record.storage.files[0]
            if (
                descriptor.data_ref.uri != stored.uri
                or descriptor.data_ref.sha256 != stored.sha256
                or descriptor.data_ref.media_type != stored.media_type
            ):
                raise ArtifactSchemaMismatchError("Tensor data_ref 与 StoredFile 不匹配")

    plan_artifact_id = manifest.execution_summary.get("plan_artifact_id")
    if plan_artifact_id is not None:
        plan_record = records[str(plan_artifact_id)]
        plan_path = _resolve_stored_path(
            root, plan_record.storage.files[0].uri, "metadata",
        )
        try:
            from pixelprobe.engine.planner import ExecutionPlan

            plan = ExecutionPlan.model_validate(_load_bounded_json(plan_path))
        except Exception as exc:
            raise ArtifactSchemaMismatchError("ExecutionPlan Artifact 无效") from exc
        if plan.plan_id == "":
            raise ArtifactSchemaMismatchError("ExecutionPlan.plan_id 不能为空")

    events_artifact_id = manifest.execution_summary.get("events_artifact_id")
    if events_artifact_id is not None:
        events_record = records[str(events_artifact_id)]
        events_path = _resolve_stored_path(
            root, events_record.storage.files[0].uri, "logs",
        )
        try:
            if events_path.stat().st_size > _MAX_METADATA_BYTES:
                raise ValueError("事件日志超过大小限制")
            event_lines = events_path.read_text("utf-8").splitlines()
            parsed_events = tuple(json.loads(line) for line in event_lines)
            if not all(isinstance(event, dict) for event in parsed_events):
                raise ValueError("事件必须是 JSON Object")
        except Exception as exc:
            raise ArtifactSchemaMismatchError("Execution Event Log 无效") from exc
        if manifest.execution_summary.get("event_count") != len(parsed_events):
            raise ArtifactSchemaMismatchError("execution_summary.event_count 不匹配")

    provenance_record = records[manifest.provenance_artifact_id]
    if provenance_record.kind != "metadata" or provenance_record.role != "provenance_graph":
        raise ProvenanceGraphInvalidError("provenance_artifact_id 指向错误类型")
    provenance_path = _resolve_stored_path(
        root, provenance_record.storage.files[0].uri, "metadata",
    )
    try:
        graph = ProvenanceGraph.model_validate(
            _load_bounded_json(provenance_path)
        )
    except Exception as exc:
        raise ProvenanceGraphInvalidError("provenance 图无法解析") from exc
    nodes = {node.provenance_id: node for node in graph.nodes}
    known_artifacts = set(records)
    producer: dict[str, str] = {}
    for node in graph.nodes:
        if not set(node.input_artifact_ids + node.output_artifact_ids) <= known_artifacts:
            raise ProvenanceGraphInvalidError("provenance 引用了缺失 Artifact")
        for artifact_id in node.output_artifact_ids:
            if artifact_id in producer:
                raise ProvenanceGraphInvalidError("Artifact 存在多个 provenance 生产者")
            producer[artifact_id] = node.provenance_id
    for record in manifest.artifacts:
        if record.kind in {"data", "preview"}:
            if record.provenance_id not in nodes or producer.get(record.artifact_id) != record.provenance_id:
                raise ProvenanceGraphInvalidError("Artifact provenance_id 与图不一致")
            if record.kind == "preview":
                node = nodes[record.provenance_id]
                if not any(
                    records[artifact_id].kind == "data"
                    for artifact_id in node.input_artifact_ids
                ):
                    raise ProvenanceGraphInvalidError(
                        "Preview provenance 必须引用源 DataArtifact"
                    )
        elif record.kind == "index" and record.role == "validity_mask":
            if (
                record.provenance_id not in nodes
                or producer.get(record.artifact_id) != record.provenance_id
            ):
                raise ProvenanceGraphInvalidError(
                    "Validity Index provenance_id 与图不一致"
                )
    dependencies = {
        node.provenance_id: {
            producer[item] for item in node.input_artifact_ids if item in producer
        }
        for node in graph.nodes
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ProvenanceGraphInvalidError("provenance 图包含环")
        visiting.add(node_id)
        for dependency in dependencies[node_id]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in dependencies:
        visit(node_id)


@dataclass(slots=True, frozen=True)
class Bundle:
    root: Path
    manifest: BundleManifest
    notices: tuple[str, ...] = ()

    def artifact(self, artifact_id: str) -> ArtifactRecord:
        for record in self.manifest.artifacts:
            if record.artifact_id == artifact_id:
                return record
        raise KeyError(artifact_id)

    def open_tensor(self, artifact_id: str) -> TensorField:
        data_record = self.artifact(artifact_id)
        if data_record.kind != "data" or data_record.tensor_schema_artifact_id is None:
            raise ArtifactSchemaMismatchError(f"{artifact_id} 不是 Tensor DataArtifact")
        schema_record = self.artifact(data_record.tensor_schema_artifact_id)
        if schema_record.kind != "metadata" or schema_record.role != "tensor_schema":
            raise ArtifactSchemaMismatchError("Tensor Schema Artifact 类型错误")
        schema_path = _resolve_stored_path(
            self.root, schema_record.storage.files[0].uri, "metadata"
        )
        try:
            schema = TensorSchemaRecord.model_validate(
                _load_bounded_json(schema_path)
            )
        except Exception as exc:
            raise ArtifactSchemaMismatchError("Tensor Schema 无效") from exc
        descriptor = schema.descriptor
        data_handle: NpyArrayHandle | ZarrArrayHandle | None = None
        validity_handle: NpyArrayHandle | None = None
        try:
            if data_record.storage.format == "npy":
                data_path = _resolve_stored_path(
                    self.root, data_record.storage.files[0].uri, "artifacts"
                )
                data_handle = NpyArrayHandle(data_path)
            elif data_record.storage.format == "zarr":
                metadata_file = next(
                    (
                        item for item in data_record.storage.files
                        if PurePosixPath(item.uri).name == "zarr.json"
                    ),
                    None,
                )
                if metadata_file is None:
                    raise ArtifactSchemaMismatchError("Zarr Artifact 缺少 zarr.json")
                zarr_metadata = _resolve_stored_path(
                    self.root, metadata_file.uri, "artifacts"
                )
                data_handle = ZarrArrayHandle(zarr_metadata.parent)
            else:
                raise ArtifactSchemaMismatchError("DataArtifact 不是 NPY 或 Zarr")
            mappings: tuple = ()
            if self.manifest.mappings_artifact_id is not None:
                mapping_record = self.artifact(self.manifest.mappings_artifact_id)
                mapping_path = _resolve_stored_path(
                    self.root, mapping_record.storage.files[0].uri, "indexes"
                )
                try:
                    all_mappings = MappingCollection.model_validate(
                        _load_bounded_json(mapping_path)
                    )
                except Exception as exc:
                    raise ArtifactSchemaMismatchError("AxisMapping 集合无效") from exc
                wanted = set(data_record.mapping_ids)
                mappings = tuple(
                    item for item in all_mappings.mappings if item.mapping_id in wanted
                )
            if (
                descriptor.data_ref.artifact_id != data_record.artifact_id
                or descriptor.shape != data_handle.shape
                or data_record.storage.shape != data_handle.shape
                or np.dtype(descriptor.dtype) != np.dtype(data_handle.dtype)
                or np.dtype(data_record.storage.dtype) != np.dtype(data_handle.dtype)
                or descriptor.mapping_ids != data_record.mapping_ids
                or len(mappings) != len(data_record.mapping_ids)
                or (
                    data_record.storage.format == "zarr"
                    and data_record.storage.chunk_shape != data_handle.chunk_shape
                )
            ):
                raise ArtifactSchemaMismatchError("Tensor Schema、Artifact 与数组不一致")
            if descriptor.validity_ref is not None:
                validity_record = self.artifact(descriptor.validity_ref.artifact_id)
                if (
                    validity_record.kind != "index"
                    or validity_record.role != "validity_mask"
                    or validity_record.storage.format != "npy"
                    or len(validity_record.storage.files) != 1
                    or validity_record.storage.shape is None
                    or validity_record.storage.dtype is None
                    or validity_record.attributes.get("source_tensor_id")
                    != descriptor.tensor_id
                    or validity_record.attributes.get("source_data_artifact_id")
                    != data_record.artifact_id
                ):
                    raise ArtifactSchemaMismatchError("validity_ref 指向无效 Validity Index")
                validity_file = validity_record.storage.files[0]
                if (
                    descriptor.validity_ref.uri != validity_file.uri
                    or descriptor.validity_ref.sha256 != validity_file.sha256
                    or descriptor.validity_ref.media_type != validity_file.media_type
                ):
                    raise ArtifactSchemaMismatchError("validity_ref 与 IndexArtifact 不一致")
                validity_path = _resolve_stored_path(
                    self.root, validity_file.uri, "indexes"
                )
                validity_handle = NpyArrayHandle(validity_path)
                if (
                    np.dtype(validity_handle.dtype) != np.dtype(bool)
                    or np.dtype(validity_record.storage.dtype) != np.dtype(bool)
                    or validity_handle.shape != validity_record.storage.shape
                ):
                    raise ArtifactSchemaMismatchError("Validity Index dtype/shape 无效")
                try:
                    np.broadcast_shapes(validity_handle.shape, data_handle.shape)
                except ValueError as exc:
                    raise ArtifactSchemaMismatchError(
                        "Validity Index 不能广播到 Tensor data"
                    ) from exc
            return TensorField(
                tensor_id=descriptor.tensor_id,
                data=data_handle,
                axes=descriptor.axes,
                channels=descriptor.channels,
                coordinate_space=schema.coordinate_space,
                axis_mappings=mappings,
                validity=validity_handle,
                accuracy=descriptor.accuracy,
                provenance=ProvenanceRef(provenance_id=descriptor.provenance_id),
                attributes=descriptor.attributes,
            )
        except Exception:
            for handle in (validity_handle, data_handle):
                close = getattr(handle, "close", None)
                if callable(close):
                    close()
            raise


class BundleReader:
    def open(
        self, root: Path, *, verify: str = "full", strict: bool = False,
    ) -> Bundle:
        if verify not in {"full", "metadata"}:
            raise ValueError("verify 必须是 full 或 metadata")
        unresolved_root = Path(root)
        if unresolved_root.is_symlink() or _is_reparse_point(unresolved_root):
            raise BundlePathUnsafeError(f"Bundle 根目录不能是链接：{unresolved_root}")
        root = unresolved_root.resolve(strict=True)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise BundleManifestMissingError(f"缺少 manifest：{manifest_path}")
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise BundleManifestInvalidError("manifest 超过 16 MiB 限制")
        try:
            raw_manifest = _load_bounded_json(
                manifest_path, max_bytes=_MAX_MANIFEST_BYTES,
            )
            if raw_manifest.get("complete") is not True:
                raise BundleIncompleteError("Bundle manifest complete 不是 true")
            manifest = BundleManifest.model_validate(raw_manifest)
        except BundleIncompleteError:
            raise
        except ValidationError as exc:
            raise BundleManifestInvalidError(f"manifest 无效：{exc}") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise BundleManifestInvalidError(f"manifest 无效：{exc}") from exc
        major, minor, _ = (int(part) for part in manifest.schema_version.split("."))
        if major != 0 or minor > 1:
            raise BundleSchemaUnsupportedError(
                f"不支持 Bundle Schema {manifest.schema_version}"
            )
        for record in manifest.artifacts:
            expected_top = _TOP_LEVEL[record.kind]
            for stored in record.storage.files:
                path = _resolve_stored_path(root, stored.uri, expected_top)
                if not path.is_file():
                    raise ArtifactFileMissingError(f"Artifact 文件缺失：{stored.uri}")
                if path.stat().st_size != stored.size_bytes:
                    raise ArtifactChecksumMismatchError(f"Artifact 大小不匹配：{stored.uri}")
                if verify == "full" and sha256_file(path) != stored.sha256:
                    raise ArtifactChecksumMismatchError(f"Artifact SHA-256 不匹配：{stored.uri}")
            if record.storage.format == "npy":
                path = _resolve_stored_path(root, record.storage.files[0].uri, expected_top)
                try:
                    with NpyArrayHandle(path) as handle:
                        if (
                            handle.shape != record.storage.shape
                            or np.dtype(handle.dtype) != np.dtype(record.storage.dtype)
                        ):
                            raise ArtifactSchemaMismatchError(
                                f"NPY shape/dtype 与 manifest 不一致：{record.artifact_id}"
                            )
                except (ValueError, OSError) as exc:
                    if isinstance(exc, ArtifactSchemaMismatchError):
                        raise
                    raise ArtifactSchemaMismatchError(f"NPY 无效：{record.artifact_id}") from exc
        _validate_semantics(root, manifest)
        registered = {"manifest.json"}
        registered.update(
            stored.uri for record in manifest.artifacts for stored in record.storage.files
        )
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*") if path.is_file()
        }
        extras = tuple(sorted(actual - registered))
        notices = tuple(
            [f"忽略未知可选字段：{path}" for path in _unknown_field_notices(manifest)]
            + [f"存在未登记文件：{path}" for path in extras]
        )
        if strict and notices:
            raise BundleManifestInvalidError("严格验证失败：" + "；".join(notices))
        return Bundle(root=root, manifest=manifest, notices=notices)


class BundleWriter:
    def write(
        self,
        target: Path,
        tensors: tuple[TensorField, ...],
        *,
        requests: tuple[RepresentationRequest, ...] = (),
        sources: tuple[SourceRecord, ...] = (),
        overwrite: bool = False,
        array_format: str = "npy",
        zarr_chunk_shape: tuple[int, ...] | None = None,
        execution_plan: Mapping[str, object] | None = None,
        events: tuple[Mapping[str, object], ...] = (),
        execution_id: str | None = None,
    ) -> Bundle:
        if not tensors:
            raise BundleWriteError("Bundle 至少需要一个 TensorField")
        if array_format not in {"npy", "zarr"}:
            raise BundleWriteError("array_format 必须是 npy 或 zarr")
        if array_format == "npy" and zarr_chunk_shape is not None:
            raise BundleWriteError("只有 Zarr 可以设置 zarr_chunk_shape")
        target = Path(target).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target) and not overwrite:
            raise BundleTargetExistsError(f"目标已存在：{target}")
        temporary = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex}"
        backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
        source_policy_by_id = _source_metadata_policies(requests, sources)
        source_path_replacements = _source_path_replacements(
            requests, sources, source_policy_by_id,
        )
        try:
            for name in ("artifacts", "previews", "indexes", "metadata", "logs"):
                (temporary / name).mkdir(parents=True)
            records: list[ArtifactRecord] = []
            mappings: dict[str, object] = {}
            provenance_nodes: list[ProvenanceNode] = []
            data_ids_by_node: dict[str, list[str]] = {}
            data_ids_by_tensor_id: dict[str, list[str]] = {}
            tensor_ids: list[tuple[str, str, str, str]] = []
            for ordinal, tensor in enumerate(tensors):
                dag_node_id = str(tensor.attributes.get("dag_node_id", ""))
                identity = dag_node_id or f"standalone_{ordinal}"
                data_id = stable_id("a", tensor.tensor_id, identity, "data")
                schema_id = stable_id("a", tensor.tensor_id, identity, "schema")
                provenance_id = stable_id(
                    "p", tensor.provenance.provenance_id, identity, tensor.tensor_id
                )
                tensor_ids.append((dag_node_id, data_id, schema_id, provenance_id))
                is_preview = tensor.attributes.get("artifact_role") == "preview"
                if dag_node_id and not is_preview:
                    data_ids_by_node.setdefault(dag_node_id, []).append(data_id)
                if not is_preview:
                    data_ids_by_tensor_id.setdefault(tensor.tensor_id, []).append(data_id)
            for tensor, identity_record in zip(tensors, tensor_ids):
                persisted_dtype = np.dtype(tensor.data.dtype).newbyteorder("<").str
                dag_node_id, data_id, schema_id, tensor_provenance_id = identity_record
                is_preview = tensor.attributes.get("artifact_role") == "preview"
                if is_preview:
                    preview = tensor.data.materialize()
                    if (
                        preview.dtype != np.uint8
                        or preview.ndim != 3
                        or preview.shape[2] not in {3, 4}
                    ):
                        raise BundleWriteError(
                            f"Preview 必须是 uint8 RGB/RGBA：{tensor.tensor_id}"
                        )
                    data_path = temporary / "previews" / data_id / "preview.png"
                    data_path.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(preview).save(
                        data_path, format="PNG", optimize=False, compress_level=9
                    )
                    with data_path.open("r+b") as preview_file:
                        os.fsync(preview_file.fileno())
                    data_file = _stored(temporary, data_path, "image/png")
                    data_files = (data_file,)
                    artifact_kind = "preview"
                    storage_format = "png"
                    stored_chunk_shape = None
                    compression = None
                    data_ref_uri = data_file.uri
                    data_ref_sha256 = data_file.sha256
                    data_ref_media_type = data_file.media_type
                elif array_format == "zarr":
                    data_path = temporary / "artifacts" / data_id / "data.zarr"
                    chunk_shape = zarr_chunk_shape or _default_chunk_shape(
                        tensor.data.shape, tensor.data.dtype
                    )
                    handle = save_array_handle_zarr(
                        tensor.data,
                        data_path,
                        chunk_shape=chunk_shape,
                        target_dtype=persisted_dtype,
                    )
                    del handle
                    data_files = _stored_tree(temporary, data_path)
                    artifact_kind = "data"
                    storage_format = "zarr"
                    stored_chunk_shape = chunk_shape
                    zarr_metadata = json.loads(
                        (data_path / "zarr.json").read_text(encoding="utf-8")
                    )
                    compression = {
                        "zarr": _package_version("zarr"),
                        "codecs": zarr_metadata.get("codecs", []),
                        "chunk_key_encoding": zarr_metadata.get("chunk_key_encoding"),
                    }
                    data_ref_uri = data_path.relative_to(temporary).as_posix()
                    data_ref_sha256 = _merkle_sha256(data_files)
                    data_ref_media_type = "application/vnd.zarr"
                else:
                    data_path = temporary / "artifacts" / data_id / "data.npy"
                    handle = save_array_handle_npy(
                        tensor.data, data_path, target_dtype=persisted_dtype,
                    )
                    handle.close()
                    data_file = _stored(temporary, data_path, "application/x-npy")
                    data_files = (data_file,)
                    artifact_kind = "data"
                    storage_format = "npy"
                    stored_chunk_shape = None
                    compression = None
                    data_ref_uri = data_file.uri
                    data_ref_sha256 = data_file.sha256
                    data_ref_media_type = data_file.media_type
                data_ref = ArtifactRef(
                    artifact_id=data_id,
                    media_type=data_ref_media_type,
                    uri=data_ref_uri,
                    sha256=data_ref_sha256,
                    schema_version=BUNDLE_SCHEMA_VERSION,
                )
                index_references: dict[str, ArtifactRef] = {}
                index_artifact_ids: list[str] = []
                validity_ref: ArtifactRef | None = None
                if tensor.validity is not None:
                    validity_array = np.ascontiguousarray(
                        tensor.validity.materialize(), dtype=bool,
                    )
                    validity_dtype = np.dtype(validity_array.dtype).newbyteorder("<").str
                    validity_id = stable_id("a", data_id, "validity", "index")
                    validity_path = temporary / "indexes" / validity_id / "data.npy"
                    validity_handle = save_array_handle_npy(
                        MemoryArrayHandle(validity_array),
                        validity_path,
                        target_dtype=validity_dtype,
                    )
                    validity_handle.close()
                    validity_file = _stored(
                        temporary, validity_path, "application/x-npy",
                    )
                    validity_ref = ArtifactRef(
                        artifact_id=validity_id,
                        media_type=validity_file.media_type,
                        uri=validity_file.uri,
                        sha256=validity_file.sha256,
                        schema_version=BUNDLE_SCHEMA_VERSION,
                    )
                    index_artifact_ids.append(validity_id)
                    records.append(ArtifactRecord(
                        artifact_id=validity_id,
                        kind="index",
                        role="validity_mask",
                        storage=StorageDescriptor(
                            format="npy",
                            files=(validity_file,),
                            dtype=validity_dtype,
                            shape=validity_array.shape,
                        ),
                        provenance_id=tensor_provenance_id,
                        complete=True,
                        attributes={
                            "source_tensor_id": tensor.tensor_id,
                            "source_data_artifact_id": data_id,
                        },
                    ))
                raw_index_values = dict(tensor.attributes.get("_index_values") or {})
                for original_id in sorted(raw_index_values):
                    index_array = np.ascontiguousarray(raw_index_values[original_id])
                    if index_array.dtype.hasobject:
                        raise BundleWriteError(
                            f"Index 不能使用 object dtype：{original_id}"
                        )
                    index_dtype = np.dtype(index_array.dtype).newbyteorder("<").str
                    index_id = stable_id("a", data_id, str(original_id), "index")
                    index_path = temporary / "indexes" / index_id / "data.npy"
                    index_handle = save_array_handle_npy(
                        MemoryArrayHandle(index_array),
                        index_path,
                        target_dtype=index_dtype,
                    )
                    index_handle.close()
                    index_file = _stored(
                        temporary, index_path, "application/x-npy",
                    )
                    index_references[str(original_id)] = ArtifactRef(
                        artifact_id=index_id,
                        media_type=index_file.media_type,
                        uri=index_file.uri,
                        sha256=index_file.sha256,
                        schema_version=BUNDLE_SCHEMA_VERSION,
                    )
                    index_artifact_ids.append(index_id)
                    records.append(ArtifactRecord(
                        artifact_id=index_id,
                        kind="index",
                        role="axis_coordinates",
                        storage=StorageDescriptor(
                            format="npy",
                            files=(index_file,),
                            dtype=index_dtype,
                            shape=index_array.shape,
                        ),
                        provenance_id=tensor_provenance_id,
                        complete=True,
                        attributes={
                            "source_tensor_id": tensor.tensor_id,
                            "original_reference_id": str(original_id),
                        },
                    ))
                updated_axes = []
                for axis in tensor.axes:
                    coordinates_ref = axis.coordinates_ref
                    if coordinates_ref is not None:
                        persisted_ref = index_references.get(
                            coordinates_ref.artifact_id,
                        )
                        if persisted_ref is None:
                            raise BundleWriteError(
                                f"坐标引用没有可持久化数值：{coordinates_ref.artifact_id}"
                            )
                        axis = axis.model_copy(update={
                            "coordinates_ref": persisted_ref,
                        })
                    updated_axes.append(axis)
                mapping_id_replacements = {
                    mapping.mapping_id: stable_id(
                        "m", data_id, mapping.mapping_id,
                    )
                    for mapping in tensor.axis_mappings
                }
                updated_axes = [
                    axis.model_copy(update={
                        "mapping_id": mapping_id_replacements.get(
                            axis.mapping_id, axis.mapping_id,
                        ),
                    })
                    for axis in updated_axes
                ]
                reference_id_replacements = {
                    original_id: reference.artifact_id
                    for original_id, reference in index_references.items()
                }
                dependency_data_ids = {
                    artifact_id
                    for node_id in tensor.attributes.get("dag_input_node_ids", ())
                    for artifact_id in data_ids_by_node.get(str(node_id), ())
                }
                updated_mappings = []
                for mapping in tensor.axis_mappings:
                    input_artifact_id = mapping.input_artifact_id
                    candidates = data_ids_by_tensor_id.get(input_artifact_id, [])
                    if candidates:
                        selected = [
                            candidate for candidate in candidates
                            if candidate in dependency_data_ids
                        ] or candidates
                        if len(selected) != 1:
                            raise BundleWriteError(
                                f"AxisMapping 输入 Artifact 无法唯一解析：{input_artifact_id}"
                            )
                        input_artifact_id = selected[0]
                    updated = mapping.model_copy(update={
                        "mapping_id": mapping_id_replacements[mapping.mapping_id],
                        "input_artifact_id": input_artifact_id,
                        "output_artifact_id": data_id,
                        "child_mapping_ids": tuple(
                            mapping_id_replacements.get(item, item)
                            for item in mapping.child_mapping_ids
                        ),
                        "parameters": _replace_reference_ids(
                            mapping.parameters, reference_id_replacements,
                        ),
                    })
                    if updated.mapping_id in mappings:
                        raise BundleWriteError(
                            f"Bundle AxisMapping ID 冲突：{updated.mapping_id}"
                        )
                    mappings[updated.mapping_id] = updated
                    updated_mappings.append(updated)
                persisted_attributes = {
                    key: value for key, value in tensor.attributes.items()
                    if key != "_index_values"
                }
                descriptor = TensorFieldDescriptor(
                    schema_version=BUNDLE_SCHEMA_VERSION,
                    tensor_id=tensor.tensor_id,
                    data_ref=data_ref,
                    dtype=persisted_dtype,
                    shape=tensor.data.shape,
                    axes=tuple(updated_axes),
                    channels=tensor.channels,
                    coordinate_space_id=(
                        tensor.coordinate_space.coordinate_space_id
                        if tensor.coordinate_space is not None else None
                    ),
                    mapping_ids=tuple(
                        mapping.mapping_id for mapping in updated_mappings
                    ),
                    validity_ref=validity_ref,
                    accuracy=tensor.accuracy,
                    provenance_id=tensor_provenance_id,
                    attributes=persisted_attributes,
                )
                schema_path = temporary / "metadata" / schema_id / "tensor.json"
                _write_bytes(schema_path, _json_bytes(TensorSchemaRecord(
                    descriptor=descriptor, coordinate_space=tensor.coordinate_space,
                )))
                schema_file = _stored(temporary, schema_path, "application/json")
                records.extend((
                    ArtifactRecord(
                        artifact_id=data_id, kind=artifact_kind, role=tensor.tensor_id,
                        storage=StorageDescriptor(
                            format=storage_format, files=data_files, dtype=persisted_dtype,
                            shape=tensor.data.shape,
                            chunk_shape=stored_chunk_shape,
                            compression=compression,
                        ),
                        tensor_schema_artifact_id=schema_id,
                        mapping_ids=descriptor.mapping_ids,
                        provenance_id=tensor_provenance_id,
                        complete=True,
                        attributes={
                            "tensor_id": tensor.tensor_id,
                            "dag_node_id": dag_node_id,
                        },
                    ),
                    ArtifactRecord(
                        artifact_id=schema_id, kind="metadata", role="tensor_schema",
                        storage=StorageDescriptor(format="json", files=(schema_file,)),
                        provenance_id=tensor_provenance_id,
                        complete=True,
                    ),
                ))
                known_data_ids = {
                    artifact_id
                    for artifact_ids in data_ids_by_tensor_id.values()
                    for artifact_id in artifact_ids
                }
                provenance_inputs = set(dependency_data_ids)
                provenance_inputs.update(
                    mapping.input_artifact_id
                    for mapping in updated_mappings
                    if mapping.input_artifact_id in known_data_ids
                )
                provenance_nodes.append(ProvenanceNode(
                    provenance_id=tensor_provenance_id,
                    node_id=tensor.tensor_id,
                    operator_name=str(tensor.attributes.get("operator", "legacy_adapter")),
                    operator_version=str(tensor.attributes.get("operator_version", "1.0.0")),
                    config={
                        **dict(tensor.attributes.get("operator_config") or {}),
                        "runtime_dependencies": dict(
                            tensor.attributes.get("runtime_dependencies") or {}
                        ),
                    },
                    input_artifact_ids=tuple(sorted(provenance_inputs)),
                    output_artifact_ids=(data_id, *index_artifact_ids),
                    dtype=tensor.data.dtype,
                ))
            mapping_id = stable_id("a", "bundle", "mappings", "index")
            mapping_path = temporary / "indexes" / mapping_id / "mappings.json"
            _write_bytes(mapping_path, _json_bytes(MappingCollection(
                mappings=tuple(mappings[key] for key in sorted(mappings)),
            )))
            records.append(ArtifactRecord(
                artifact_id=mapping_id, kind="index", role="axis_mappings",
                storage=StorageDescriptor(
                    format="json", files=(_stored(temporary, mapping_path, "application/json"),),
                ), complete=True,
            ))
            provenance_id = stable_id("a", "bundle", "provenance", "metadata")
            provenance_path = temporary / "metadata" / provenance_id / "provenance.json"
            _write_bytes(provenance_path, _json_bytes(ProvenanceGraph(
                nodes=tuple(provenance_nodes),
            )))
            records.append(ArtifactRecord(
                artifact_id=provenance_id, kind="metadata", role="provenance_graph",
                storage=StorageDescriptor(
                    format="json", files=(_stored(temporary, provenance_path, "application/json"),),
                ), complete=True,
            ))
            plan_artifact_id: str | None = None
            if execution_plan is not None:
                plan_artifact_id = stable_id("a", "execution", "plan", "metadata")
                plan_path = temporary / "metadata" / plan_artifact_id / "plan.json"
                _write_bytes(
                    plan_path,
                    _json_bytes(_redact_source_paths(
                        dict(execution_plan), source_path_replacements,
                    )),
                )
                records.append(ArtifactRecord(
                    artifact_id=plan_artifact_id,
                    kind="metadata",
                    role="execution_plan",
                    storage=StorageDescriptor(
                        format="json",
                        files=(_stored(
                            temporary, plan_path, "application/json",
                        ),),
                    ),
                    complete=True,
                ))
            events_artifact_id: str | None = None
            if events:
                events_artifact_id = stable_id("a", "execution", "events", "log")
                events_path = temporary / "logs" / events_artifact_id / "events.jsonl"
                event_bytes = b"".join(
                    (json.dumps(
                        _redact_source_paths(
                            dict(event), source_path_replacements,
                        ), ensure_ascii=False, allow_nan=False,
                        separators=(",", ":"), sort_keys=True,
                    ) + "\n").encode("utf-8")
                    for event in events
                )
                _write_bytes(events_path, event_bytes)
                records.append(ArtifactRecord(
                    artifact_id=events_artifact_id,
                    kind="log",
                    role="execution_events",
                    storage=StorageDescriptor(
                        format="text",
                        files=(_stored(
                            temporary, events_path, "application/x-ndjson",
                        ),),
                    ),
                    complete=True,
                ))
            manifest_requests = tuple(
                request if source_policy_by_id.get(
                    request.source.source_id, "safe"
                ) == "full" else request.model_copy(update={
                    "source": request.source.model_copy(update={
                        "uri": f"source://{request.source.source_id}",
                        "sequence_manifest": (
                            f"source://{request.source.source_id}/manifest"
                            if request.source.kind == "image_sequence" else None
                        ),
                    }),
                })
                for request in requests
            )
            manifest_sources = tuple(
                source if source_policy_by_id.get(source.source_id, "safe") == "full"
                else source.model_copy(update={
                    "original_uri": None,
                    "metadata_policy": (
                        source.metadata_policy
                        if source.metadata_policy != "full" else "safe"
                    ),
                })
                for source in sources
            )
            manifest = BundleManifest(
                schema_version=BUNDLE_SCHEMA_VERSION,
                bundle_id=_uuid7(),
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                complete=True,
                producer=_producer(),
                sources=tuple(sorted(manifest_sources, key=lambda item: item.source_id)),
                requests=manifest_requests,
                artifacts=tuple(sorted(records, key=lambda item: item.artifact_id)),
                mappings_artifact_id=mapping_id,
                provenance_artifact_id=provenance_id,
                execution_summary={
                    "execution_id": execution_id or _uuid7(),
                    "status": "succeeded",
                    "backend": "cpu",
                    "plan_artifact_id": plan_artifact_id,
                    "events_artifact_id": events_artifact_id,
                    "event_count": len(events),
                },
                warnings=tuple(
                    {
                        "code": "FULL_METADATA_PRIVACY_RISK",
                        "severity": "warning",
                        "source_id": source.source_id,
                    }
                    for source in sorted(manifest_sources, key=lambda item: item.source_id)
                    if source_policy_by_id.get(source.source_id, "safe") == "full"
                ),
            )
            _write_bytes(temporary / "manifest.json", _json_bytes(manifest))
            BundleReader().open(temporary, verify="full")
            if overwrite:
                if os.path.lexists(target):
                    os.replace(target, backup)
                os.replace(temporary, target)
            else:
                if os.path.lexists(target):
                    raise BundleTargetExistsError(f"目标已存在：{target}")
                try:
                    # 不调用 os.replace；有效 Bundle 是非空目录，若其他写入方
                    # 在长任务期间抢先提交，rename 会失败并保留其结果。
                    os.rename(temporary, target)
                except OSError as exc:
                    if os.path.lexists(target):
                        raise BundleTargetExistsError(
                            f"目标已存在：{target}"
                        ) from exc
                    raise
            if backup.exists():
                shutil.rmtree(backup)
            return BundleReader().open(target, verify="full")
        except BundleTargetExistsError:
            raise
        except Exception as exc:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            if isinstance(exc, BundleWriteError):
                raise
            raise BundleWriteError(f"Bundle 写入失败：{exc}") from exc
        finally:
            if temporary.exists():
                _remove_or_abandon(temporary)
