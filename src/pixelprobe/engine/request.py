"""CLI、Python API 与 Bundle 共用的表示请求。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.domain.geometry import Geometry, PathGeometry
from pixelprobe.domain.media import MediaSource
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.operators.base import ResourcePolicy


class FeatureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    config: dict[str, object] = Field(default_factory=dict)


class ReductionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["mean", "min", "max", "median", "std", "rms", "percentile", "diff"]
    axes: tuple[str, ...]
    config: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_reduction(self) -> "ReductionRequest":
        if self.axes != ("time",):
            raise ValueError("1.0 时间聚合只支持 axes=['time']")
        percentile = self.config.get("percentile")
        if self.name == "percentile":
            if percentile is None or not 0 <= float(percentile) <= 100:
                raise ValueError("percentile 聚合需要 0～100 的 config.percentile")
        elif percentile is not None:
            raise ValueError("只有 percentile 聚合可以设置 config.percentile")
        return self


class OutputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal["memory", "npy", "zarr", "bundle"] = "bundle"
    include_preview: bool = True
    preview_config: dict[str, object] = Field(default_factory=dict)
    metadata_policy: Literal["safe", "standard", "full"] = "safe"


class RepresentationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="0.1.0", pattern=r"^0\.\d+\.\d+$")
    source: MediaSource
    selection: TemporalSelection
    representation: Literal[
        "frames", "xt", "yt", "points_t", "path_t", "roi_t",
        "feature_t",
    ]
    geometry: Geometry | None = None
    feature: FeatureRequest = Field(default_factory=lambda: FeatureRequest(name="rgb"))
    reduction: ReductionRequest | None = None
    output: OutputRequest = Field(default_factory=OutputRequest)
    resources: ResourcePolicy = Field(
        default_factory=lambda: ResourcePolicy(max_memory_bytes=268_435_456)
    )

    @model_validator(mode="after")
    def validate_geometry(self) -> "RepresentationRequest":
        if self.source.kind != "file":
            raise ValueError("PixelProbe 1.0 本地 Executor 只支持 file MediaSource")
        expected = {
            "xt": {"line", "polyline"},
            "yt": {"line", "polyline"},
            "points_t": {"point", "line", "polyline"},
            "path_t": {"line", "polyline", "curve"},
            "roi_t": {"rect"},
        }
        if self.representation in expected:
            if self.geometry is None or self.geometry.type not in expected[self.representation]:
                kinds = ", ".join(sorted(expected[self.representation]))
                raise ValueError(f"{self.representation} 需要 {kinds} 几何")
            if self.representation == "xt":
                assert isinstance(self.geometry, PathGeometry)
                y_values = {point[1] for point in self.geometry.points}
                if len(y_values) != 1:
                    raise ValueError("xt 必须使用水平 line/polyline")
            if self.representation == "yt":
                assert isinstance(self.geometry, PathGeometry)
                x_values = {point[0] for point in self.geometry.points}
                if len(x_values) != 1:
                    raise ValueError("yt 必须使用垂直 line/polyline")
        elif self.geometry is not None and self.representation == "frames":
            raise ValueError("frames 表示不能设置 geometry")
        if self.representation == "roi_t" and self.reduction is None:
            raise ValueError("roi_t 必须提供 reduction")
        return self


def merge_resource_policies(
    policies: tuple[ResourcePolicy, ...],
) -> ResourcePolicy:
    """按最严格语义合并一次多请求执行的资源策略。

    ``None`` 的临时空间和超时上限表示“不额外限制”，因此只有在至少一个
    请求给出上限时才取其中最小值。``allow_partial`` 是权限放宽项，必须由
    所有请求同时允许才可启用。
    """
    if not policies:
        raise ValueError("至少需要一个 ResourcePolicy")
    temporary_limits = tuple(
        policy.max_temporary_bytes
        for policy in policies
        if policy.max_temporary_bytes is not None
    )
    timeouts = tuple(
        policy.timeout_seconds
        for policy in policies
        if policy.timeout_seconds is not None
    )
    max_memory_bytes = min(policy.max_memory_bytes for policy in policies)
    return ResourcePolicy(
        max_memory_bytes=max_memory_bytes,
        max_temporary_bytes=min(temporary_limits) if temporary_limits else None,
        timeout_seconds=min(timeouts) if timeouts else None,
        preferred_chunk_bytes=min(
            max_memory_bytes,
            *(policy.preferred_chunk_bytes for policy in policies),
        ),
        allow_partial=all(policy.allow_partial for policy in policies),
    )
