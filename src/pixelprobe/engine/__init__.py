"""PixelProbe 类型化请求与本地执行引擎。"""

from importlib import import_module

from pixelprobe.engine.request import (
    FeatureRequest,
    OutputRequest,
    ReductionRequest,
    RepresentationRequest,
)

__all__ = [
    "ComputationGraph",
    "CacheKeyInput",
    "CancellationToken",
    "CheckpointRecord",
    "ExecutionPlan",
    "FeatureRequest",
    "GenerationResult",
    "GraphBuilder",
    "LocalArrayCache",
    "LocalExecutor",
    "LocalExecutionContext",
    "NpyArtifactSink",
    "OutputRequest",
    "OperatorRegistry",
    "ReductionRequest",
    "RepresentationRequest",
    "Planner",
    "choose_chunk_shape",
    "core_view",
    "encoded_state",
    "iter_tensor_chunks",
]

_LAZY = {
    "CacheKeyInput": ("pixelprobe.engine.cache", "CacheKeyInput"),
    "LocalArrayCache": ("pixelprobe.engine.cache", "LocalArrayCache"),
    "NpyArtifactSink": ("pixelprobe.engine.chunks", "NpyArtifactSink"),
    "choose_chunk_shape": ("pixelprobe.engine.chunks", "choose_chunk_shape"),
    "core_view": ("pixelprobe.engine.chunks", "core_view"),
    "iter_tensor_chunks": ("pixelprobe.engine.chunks", "iter_tensor_chunks"),
    "CancellationToken": ("pixelprobe.engine.execution", "CancellationToken"),
    "CheckpointRecord": ("pixelprobe.engine.execution", "CheckpointRecord"),
    "LocalExecutionContext": ("pixelprobe.engine.execution", "LocalExecutionContext"),
    "encoded_state": ("pixelprobe.engine.execution", "encoded_state"),
    "ComputationGraph": ("pixelprobe.engine.graph", "ComputationGraph"),
    "GraphBuilder": ("pixelprobe.engine.graph", "GraphBuilder"),
    "ExecutionPlan": ("pixelprobe.engine.planner", "ExecutionPlan"),
    "Planner": ("pixelprobe.engine.planner", "Planner"),
    "GenerationResult": ("pixelprobe.engine.executor", "GenerationResult"),
    "LocalExecutor": ("pixelprobe.engine.executor", "LocalExecutor"),
    "OperatorRegistry": ("pixelprobe.engine.operator_registry", "OperatorRegistry"),
}


def __getattr__(name: str):
    if name not in _LAZY:
        raise AttributeError(name)
    module_name, attribute = _LAZY[name]
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
