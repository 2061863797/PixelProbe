"""精度等级与字段级来源。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccuracyLevel(StrEnum):
    EXACT = "exact"
    DECODED = "decoded"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class AccuracyInfo(BaseModel):
    """字段或结果的精度、来源与显式假设。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: AccuracyLevel
    source: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()
    tolerance: float | None = Field(default=None, ge=0)
    unit: str | None = None

    @model_validator(mode="after")
    def validate_estimate(self) -> "AccuracyInfo":
        if self.level == AccuracyLevel.ESTIMATED and not self.assumptions:
            raise ValueError("estimated 精度必须说明估算假设")
        return self
