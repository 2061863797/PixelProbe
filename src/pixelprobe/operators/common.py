"""首批 Operator 共用的 Tensor 构造工具。"""

from __future__ import annotations

import hashlib

import numpy as np

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import ChannelSpec
from pixelprobe.domain.references import ArtifactRef


def memory_array_ref(array: np.ndarray, artifact_id: str) -> ArtifactRef:
    """为执行期内存数组生成可校验引用；V0.8 持久化时替换 URI。"""
    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256(contiguous.tobytes(order="C")).hexdigest()
    return ArtifactRef(
        artifact_id=artifact_id,
        media_type="application/x-numpy-memory",
        uri=f"memory://{artifact_id}",
        sha256=digest,
        schema_version="0.1.0",
    )


def rgb_channels(source: str = "pyav") -> tuple[ChannelSpec, ...]:
    accuracy = AccuracyInfo(
        level=AccuracyLevel.DECODED,
        source=source,
        unit="code_value",
    )
    return tuple(
        ChannelSpec(
            name=name,
            unit="code_value",
            semantic=semantic,
            value_range=(0, 255),
            accuracy=accuracy,
        )
        for name, semantic in (
            ("r", "decoded_srgb_red"),
            ("g", "decoded_srgb_green"),
            ("b", "decoded_srgb_blue"),
        )
    )
