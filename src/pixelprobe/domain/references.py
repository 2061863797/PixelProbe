"""Artifact 与 provenance 的轻量引用。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProvenanceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance_id: str = Field(min_length=1)
    manifest_uri: str | None = None


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
