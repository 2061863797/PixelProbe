"""媒体来源、内容身份与运行时帧包。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pixelprobe.domain.accuracy import AccuracyInfo
from pixelprobe.domain.coordinates import TransformChain
from pixelprobe.domain.references import ProvenanceRef


class MediaSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    kind: Literal["file", "image_sequence", "frame_stream"]
    uri: str = Field(min_length=1)
    sequence_manifest: str | None = None
    declared_media_type: Literal["image", "video", "image_sequence"] | None = None

    @model_validator(mode="after")
    def validate_sequence(self) -> "MediaSource":
        if self.kind == "image_sequence" and not self.sequence_manifest:
            raise ValueError("图片序列必须提供 sequence_manifest")
        if self.kind != "image_sequence" and self.sequence_manifest is not None:
            raise ValueError("只有图片序列可以提供 sequence_manifest")
        return self


class MediaIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_id: str | None = None
    modified_time_ns: int | None = Field(default=None, ge=0)
    actual_format: str | None = None


@dataclass(slots=True)
class FramePacket:
    """解码层向计算层交付的单个展示帧。"""

    data: np.ndarray
    presentation_index: int
    decode_index: int | None
    pts: int | None
    dts: int | None
    time_base: Fraction
    source_timestamp_seconds: float | None
    timeline_time_seconds: float
    duration_pts: int | None
    duration_seconds: float | None
    key_frame: bool | None
    stored_pixel_format: str | None
    decoded_pixel_format: str
    color_metadata: dict[str, object]
    transform_chain: TransformChain
    sample_semantics: Literal["decoded_sample", "display_value"]
    accuracy: AccuracyInfo
    provenance: ProvenanceRef
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.presentation_index < 0:
            raise ValueError("presentation_index 必须 >= 0")
        if self.decode_index is not None and self.decode_index < 0:
            raise ValueError("decode_index 必须 >= 0 或为 None")
        if not isinstance(self.data, np.ndarray) or self.data.ndim < 2:
            raise ValueError("data 必须是至少二维的 NumPy 数组")
        if self.time_base <= 0:
            raise ValueError("time_base 必须为正有理数")
        if not math.isfinite(self.timeline_time_seconds) or self.timeline_time_seconds < 0:
            raise ValueError("timeline_time_seconds 必须是非负有限数值")
        if self.source_timestamp_seconds is not None and not math.isfinite(self.source_timestamp_seconds):
            raise ValueError("source_timestamp_seconds 必须是有限数值或 None")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("duration_seconds 必须 >= 0")
        self.color_metadata = dict(self.color_metadata)
        self.flags = tuple(dict.fromkeys(self.flags))
