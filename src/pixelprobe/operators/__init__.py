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
    "SamplingConfig", "SamplingOutput", "SamplingPlan", "TensorChunk",
    "TensorDescriptor",
    "execute_sampling", "resample_polyline", "sample_path_t", "sample_points_t",
    "sample_roi_t", "sample_xt", "sample_yt",
]

_SAMPLING_EXPORTS = {
    "SamplingConfig",
    "SamplingOutput",
    "SamplingPlan",
    "execute_sampling",
    "resample_polyline",
    "sample_path_t",
    "sample_points_t",
    "sample_roi_t",
    "sample_xt",
    "sample_yt",
}


def __getattr__(name: str):
    if name not in _SAMPLING_EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module("pixelprobe.operators.sampling"), name)
    globals()[name] = value
    return value
