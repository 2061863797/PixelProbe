"""Bundle manifest、Artifact 与 provenance 的公开 Schema。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.domain.axes import AxisMapping
from pixelprobe.domain.coordinates import CoordinateSpace
from pixelprobe.domain.media import MediaIdentity
from pixelprobe.domain.tensor import TensorFieldDescriptor
from pixelprobe.engine.request import RepresentationRequest

# 同一主版本允许新增可选字段；Pydantic 会保留这些字段，Reader 另行记录 notice。
_CONFIG = ConfigDict(extra="allow", frozen=True)
_ID_PATTERN = r"^[a-z0-9_-]{1,96}$"


class ProducerInfo(BaseModel):
    model_config = _CONFIG
    name: Literal["pixelprobe"] = "pixelprobe"
    version: str
    python_version: str
    platform: str
    dependencies: dict[str, str]


class SourceRecord(BaseModel):
    model_config = _CONFIG
    source_id: str = Field(pattern=_ID_PATTERN)
    media_identity: MediaIdentity
    original_uri: str | None = None
    metadata_policy: Literal["safe", "standard", "full"] = "safe"


class StoredFile(BaseModel):
    model_config = _CONFIG
    uri: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)


class StorageDescriptor(BaseModel):
    model_config = _CONFIG
    format: Literal["npy", "zarr", "json", "png", "webp", "text"]
    files: tuple[StoredFile, ...]
    dtype: str | None = None
    shape: tuple[int, ...] | None = None
    chunk_shape: tuple[int, ...] | None = None
    compression: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_storage(self) -> "StorageDescriptor":
        if not self.files:
            raise ValueError("storage.files 不能为空")
        if self.format in {"npy", "zarr", "png", "webp"}:
            if self.dtype is None or self.shape is None:
                raise ValueError("Tensor storage 必须记录 dtype 和 shape")
            if self.format in {"png", "webp"} and self.chunk_shape is not None:
                raise ValueError("图片 Preview 不能记录 chunk_shape")
        elif any(value is not None for value in (self.dtype, self.shape, self.chunk_shape)):
            raise ValueError("非数组 storage 不能记录 dtype/shape/chunk_shape")
        return self


class ArtifactRecord(BaseModel):
    model_config = _CONFIG
    artifact_id: str = Field(pattern=_ID_PATTERN)
    kind: Literal["data", "preview", "index", "metadata", "log"]
    role: str = Field(min_length=1)
    storage: StorageDescriptor
    tensor_schema_artifact_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    mapping_ids: tuple[str, ...] = ()
    provenance_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    complete: bool
    attributes: dict[str, object] = Field(default_factory=dict)


class TensorSchemaRecord(BaseModel):
    model_config = _CONFIG
    descriptor: TensorFieldDescriptor
    coordinate_space: CoordinateSpace | None = None


class ProvenanceNode(BaseModel):
    model_config = _CONFIG
    provenance_id: str = Field(pattern=_ID_PATTERN)
    node_id: str = Field(min_length=1)
    operator_name: str
    operator_version: str
    config: dict[str, object] = Field(default_factory=dict)
    input_artifact_ids: tuple[str, ...] = ()
    output_artifact_ids: tuple[str, ...]
    backend: Literal["cpu"] = "cpu"
    dtype: str
    complete: bool = True


class ProvenanceGraph(BaseModel):
    model_config = _CONFIG
    nodes: tuple[ProvenanceNode, ...]

    @model_validator(mode="after")
    def validate_graph(self) -> "ProvenanceGraph":
        ids = [node.provenance_id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("provenance_id 不能重复")
        return self


class BundleManifest(BaseModel):
    model_config = _CONFIG
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    bundle_id: str
    created_at: str
    complete: Literal[True]
    producer: ProducerInfo
    sources: tuple[SourceRecord, ...]
    requests: tuple[RepresentationRequest, ...]
    artifacts: tuple[ArtifactRecord, ...]
    mappings_artifact_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    provenance_artifact_id: str = Field(pattern=_ID_PATTERN)
    execution_summary: dict[str, object]
    warnings: tuple[dict[str, object], ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> "BundleManifest":
        ids = [record.artifact_id for record in self.artifacts]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("artifacts 必须按唯一 artifact_id 排序")
        known = set(ids)
        required = {self.provenance_artifact_id}
        if self.mappings_artifact_id is not None:
            required.add(self.mappings_artifact_id)
        for record in self.artifacts:
            if record.tensor_schema_artifact_id is not None:
                required.add(record.tensor_schema_artifact_id)
        if not required <= known:
            raise ValueError("manifest 包含缺失的 Artifact 引用")
        records_by_id = {record.artifact_id: record for record in self.artifacts}
        summary_refs = (
            ("plan_artifact_id", "metadata", "execution_plan"),
            ("events_artifact_id", "log", "execution_events"),
        )
        for field, expected_kind, expected_role in summary_refs:
            artifact_id = self.execution_summary.get(field)
            if artifact_id is None:
                continue
            record = records_by_id.get(str(artifact_id))
            if record is None:
                raise ValueError(f"execution_summary.{field} 引用了缺失 Artifact")
            if record.kind != expected_kind or record.role != expected_role:
                raise ValueError(f"execution_summary.{field} 的 Artifact 类型不匹配")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("sources.source_id 不能重复")
        source_by_id = {source.source_id: source for source in self.sources}
        for source in self.sources:
            if source.media_identity.source_id != source.source_id:
                raise ValueError("SourceRecord 与 MediaIdentity.source_id 不一致")
        for request in self.requests:
            source = source_by_id.get(request.source.source_id)
            if source is None:
                raise ValueError("请求引用了缺失的 SourceRecord")
            if source.metadata_policy != "full" and request.source.uri != (
                f"source://{source.source_id}"
            ):
                raise ValueError("safe/standard 请求不得保存原始媒体路径")
        return self


class MappingCollection(BaseModel):
    model_config = _CONFIG
    mappings: tuple[AxisMapping, ...]

    @model_validator(mode="after")
    def validate_ids(self) -> "MappingCollection":
        ids = [mapping.mapping_id for mapping in self.mappings]
        if len(ids) != len(set(ids)):
            raise ValueError("mapping_id 不能重复")
        return self
