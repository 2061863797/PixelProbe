"""统一的半开区间时间选择模型。"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TemporalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["all", "frame_interval", "time_interval", "indices"]
    requested_start_frame: int | None = Field(default=None, ge=0)
    requested_end_frame_exclusive: int | None = Field(default=None, ge=0)
    requested_start_seconds: float | None = Field(default=None, ge=0)
    requested_end_seconds: float | None = Field(default=None, ge=0)
    requested_indices: tuple[int, ...] = ()
    sample_every: int = Field(default=1, ge=1)
    resolved_presentation_indices: tuple[int, ...] = ()
    resolved_timestamps_seconds: tuple[float, ...] = ()
    mapping_id: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> "TemporalSelection":
        frame_values = (
            self.requested_start_frame,
            self.requested_end_frame_exclusive,
        )
        time_values = (
            self.requested_start_seconds,
            self.requested_end_seconds,
        )
        if self.mode == "all":
            if any(v is not None for v in frame_values + time_values) or self.requested_indices:
                raise ValueError("all 模式不能包含范围或离散索引")
        elif self.mode == "frame_interval":
            if None in frame_values or any(v is not None for v in time_values) or self.requested_indices:
                raise ValueError("frame_interval 只能填写完整帧范围")
            assert self.requested_start_frame is not None
            assert self.requested_end_frame_exclusive is not None
            if self.requested_start_frame >= self.requested_end_frame_exclusive:
                raise ValueError("帧范围必须满足 start < end_exclusive")
        elif self.mode == "time_interval":
            if None in time_values or any(v is not None for v in frame_values) or self.requested_indices:
                raise ValueError("time_interval 只能填写完整时间范围")
            assert self.requested_start_seconds is not None
            assert self.requested_end_seconds is not None
            if not all(math.isfinite(v) for v in time_values):
                raise ValueError("时间范围必须是有限数值")
            if self.requested_start_seconds >= self.requested_end_seconds:
                raise ValueError("时间范围必须满足 start < end")
        else:
            if any(v is not None for v in frame_values + time_values) or not self.requested_indices:
                raise ValueError("indices 模式必须且只能填写 requested_indices")

        for values, label in (
            (self.requested_indices, "requested_indices"),
            (self.resolved_presentation_indices, "resolved_presentation_indices"),
        ):
            if any(v < 0 for v in values) or any(a >= b for a, b in zip(values, values[1:])):
                raise ValueError(f"{label} 必须非负、严格递增且不重复")
        if len(self.resolved_presentation_indices) != len(self.resolved_timestamps_seconds):
            raise ValueError("已解析帧号与时间戳数量必须相等")
        if any(not math.isfinite(v) or v < 0 for v in self.resolved_timestamps_seconds):
            raise ValueError("已解析时间戳必须是非负有限数值")
        return self
