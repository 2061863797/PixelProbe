"""Tensor 轴、通道与输入输出映射。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.domain.accuracy import AccuracyInfo
from pixelprobe.domain.references import ArtifactRef


class AxisKind(StrEnum):
    TIME = "time"
    X = "x"
    Y = "y"
    PATH = "path"
    CHANNEL = "channel"
    FREQUENCY = "frequency"
    BATCH = "batch"
    FEATURE = "feature"


class AxisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    kind: AxisKind
    length: int = Field(ge=0)
    unit: str | None = None
    coordinate_mode: Literal["index", "regular", "irregular"] = "index"
    start: float | None = None
    step: float | None = None
    coordinates_ref: ArtifactRef | None = None
    mapping_id: str | None = None

    @model_validator(mode="after")
    def validate_coordinates(self) -> "AxisSpec":
        if self.kind == AxisKind.TIME and self.unit not in {None, "second"}:
            raise ValueError("时间轴单位必须是 second")
        if self.coordinate_mode == "index":
            if self.start is not None or self.step is not None or self.coordinates_ref is not None:
                raise ValueError("index 轴不能带规则或不规则坐标")
        elif self.coordinate_mode == "regular":
            if self.start is None or self.step in {None, 0} or self.coordinates_ref is not None:
                raise ValueError("regular 轴必须提供 start 和非零 step")
        elif self.coordinates_ref is None or self.start is not None or self.step is not None:
            raise ValueError("irregular 轴必须且只能提供 coordinates_ref")
        return self


class ChannelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    unit: str | None = None
    semantic: str = Field(min_length=1)
    value_range: tuple[float, float] | None = None
    accuracy: AccuracyInfo

    @model_validator(mode="after")
    def validate_range(self) -> "ChannelSpec":
        if self.value_range is not None and self.value_range[0] > self.value_range[1]:
            raise ValueError("value_range 下界不能大于上界")
        return self


class AxisMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mapping_id: str = Field(min_length=1)
    kind: Literal["affine", "index", "interval", "weighted", "lookup", "composite"]
    input_artifact_id: str = Field(min_length=1)
    input_axes: tuple[str, ...]
    output_artifact_id: str | None = None
    output_axes: tuple[str, ...]
    parameters: dict[str, object] = Field(default_factory=dict)
    child_mapping_ids: tuple[str, ...] = ()
    accuracy: AccuracyInfo

    @model_validator(mode="after")
    def validate_parameters(self) -> "AxisMapping":
        required = {
            "affine": {"scale", "offset"},
            "index": {"indices_ref"},
            "interval": {"starts_ref", "ends_ref"},
            "weighted": {"indices_ref", "weights_ref"},
            "lookup": {"coordinates_ref"},
            "composite": set(),
        }[self.kind]
        missing = required.difference(self.parameters)
        if missing:
            raise ValueError(f"{self.kind} 映射缺少参数：{', '.join(sorted(missing))}")
        if self.kind == "composite" and not self.child_mapping_ids:
            raise ValueError("composite 映射必须引用至少一个子映射")
        if self.kind != "composite" and self.child_mapping_ids:
            raise ValueError("只有 composite 映射可以引用子映射")
        return self
