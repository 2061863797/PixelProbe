"""PixelProbe 内部首批确定性 Operator。"""

from importlib import import_module

from pixelprobe.operators.base import (
    HaloSpec,
    Operator,
    OperatorPlan,
    OperatorSpec,
    ResourcePolicy,
    RuntimeInvocation,
    TensorChunk,
    TensorDescriptor,
)

__all__ = [
    "HaloSpec", "Operator", "OperatorPlan", "OperatorSpec", "ResourcePolicy",
    "RuntimeInvocation",
    "SamplingConfig", "SamplingPlan", "TensorChunk",
    "TensorDescriptor", "resample_polyline",
]

_SAMPLING_EXPORTS = {
    "SamplingConfig",
    "SamplingPlan",
    "resample_polyline",
}


def __getattr__(name: str):
    if name not in _SAMPLING_EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module("pixelprobe.operators.sampling"), name)
    globals()[name] = value
    return value
