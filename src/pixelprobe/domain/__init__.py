"""PixelProbe 的规范领域模型。"""

from pixelprobe.domain.accuracy import AccuracyInfo, AccuracyLevel
from pixelprobe.domain.axes import AxisKind, AxisMapping, AxisSpec, ChannelSpec
from pixelprobe.domain.coordinates import (
    CoordinateSpace,
    CoordinateSpaceKind,
    TransformChain,
    TransformStep,
)
from pixelprobe.domain.geometry import (
    Geometry,
    MaskGeometry,
    PathGeometry,
    PointGeometry,
    RectGeometry,
)
from pixelprobe.domain.media import FramePacket, MediaIdentity, MediaSource
from pixelprobe.domain.references import ArtifactRef, ProvenanceRef
from pixelprobe.domain.tensor import (
    ArrayHandle,
    MemoryArrayHandle,
    StorageKind,
    TensorField,
    TensorFieldDescriptor,
)
from pixelprobe.domain.time import TemporalSelection

__all__ = [
    "AccuracyInfo", "AccuracyLevel", "ArrayHandle", "ArtifactRef", "AxisKind",
    "AxisMapping", "AxisSpec", "ChannelSpec", "CoordinateSpace",
    "CoordinateSpaceKind", "FramePacket", "Geometry", "MaskGeometry",
    "MediaIdentity", "MediaSource", "MemoryArrayHandle", "PathGeometry",
    "PointGeometry", "ProvenanceRef", "RectGeometry", "StorageKind",
    "TemporalSelection", "TensorField", "TensorFieldDescriptor",
    "TransformChain", "TransformStep",
]
