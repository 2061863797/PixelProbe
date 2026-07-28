"""坐标空间与可追踪空间变换。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CoordinateSpaceKind(StrEnum):
    STORAGE = "storage"
    DISPLAY = "display"
    NORMALIZED = "normalized"
    CAMERA = "camera"
    WORLD = "world"


class CoordinateSpace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coordinate_space_id: str = Field(min_length=1)
    kind: CoordinateSpaceKind
    axes: tuple[str, ...]
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    unit: str = "pixel"
    parent_space_id: str | None = None

    @model_validator(mode="after")
    def validate_axes(self) -> "CoordinateSpace":
        if len(set(self.axes)) != len(self.axes):
            raise ValueError("坐标轴名称不能重复")
        if (self.width is None) != (self.height is None):
            raise ValueError("width 与 height 必须同时提供或同时省略")
        return self


class TransformStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal[
        "identity", "exif_orientation", "rotate", "scale",
        "translate", "crop", "affine", "perspective",
    ]
    parameters: dict[str, object] = Field(default_factory=dict)
    rounding: Literal["none", "nearest", "floor", "ceil"] = "none"


class TransformChain(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_space_id: str = Field(min_length=1)
    target_space_id: str = Field(min_length=1)
    steps: tuple[TransformStep, ...]
    invertible: bool

    @classmethod
    def identity(cls, coordinate_space_id: str) -> "TransformChain":
        return cls(
            source_space_id=coordinate_space_id,
            target_space_id=coordinate_space_id,
            steps=(TransformStep(operation="identity"),),
            invertible=True,
        )
