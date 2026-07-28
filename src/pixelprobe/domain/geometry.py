"""公开请求使用的几何对象。"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.domain.references import ArtifactRef


class PointGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["point"] = "point"
    coordinate_space_id: str
    x: float
    y: float


class RectGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["rect"] = "rect"
    coordinate_space_id: str
    x: float
    y: float
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class PathGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["line", "polyline", "curve"]
    coordinate_space_id: str
    points: tuple[tuple[float, float], ...]
    closed: bool = False

    @model_validator(mode="after")
    def validate_points(self) -> "PathGeometry":
        minimum = 2 if self.type in {"line", "polyline"} else 3
        if len(self.points) < minimum:
            raise ValueError(f"{self.type} 至少需要 {minimum} 个点")
        if self.type == "line" and len(self.points) != 2:
            raise ValueError("line 必须恰好包含两个点")
        return self


class MaskGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["mask"] = "mask"
    coordinate_space_id: str
    mask_ref: ArtifactRef


Geometry: TypeAlias = Annotated[
    PointGeometry | RectGeometry | PathGeometry | MaskGeometry,
    Field(discriminator="type"),
]
