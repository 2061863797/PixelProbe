"""PixelProbe 1.0 统一 Python 生成 API。"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from pixelprobe.engine.execution import LocalExecutionContext
from pixelprobe.engine.cache import LocalArrayCache
from pixelprobe.engine.executor import GenerationResult, LocalExecutor
from pixelprobe.engine.graph import GraphBuilder
from pixelprobe.engine.planner import ExecutionPlan, Planner
from pixelprobe.engine.operator_registry import OperatorRegistry
from pixelprobe.engine.request import RepresentationRequest, merge_resource_policies


RequestInput = RepresentationRequest | Mapping[str, object]


def _requests(value: RequestInput | Sequence[RequestInput]) -> tuple[RepresentationRequest, ...]:
    items = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)) else (value,)
    return tuple(
        item if isinstance(item, RepresentationRequest)
        else RepresentationRequest.model_validate(item)
        for item in items
    )


def explain(
    request: RequestInput | Sequence[RequestInput],
    *,
    registry: OperatorRegistry | None = None,
) -> ExecutionPlan:
    requests = _requests(request)
    graph = GraphBuilder().build(requests)
    return Planner(registry or OperatorRegistry()).plan(graph, requests)


def generate(
    request: RequestInput | Sequence[RequestInput],
    *,
    output_path: Path | None = None,
    temporary_root: Path | None = None,
    cache_root: Path | None = None,
    checkpoint_path: Path | None = None,
    resume_from: Path | None = None,
    registry: OperatorRegistry | None = None,
) -> GenerationResult:
    """验证请求、构建最小 DAG、规划并在本地 CPU 上执行。"""
    requests = _requests(request)
    graph = GraphBuilder().build(requests)
    operator_registry = registry or OperatorRegistry()
    plan = Planner(operator_registry).plan(graph, requests)
    resources = merge_resource_policies(
        tuple(item.resources for item in requests)
    )
    if temporary_root is not None:
        context = LocalExecutionContext(
            resources, Path(temporary_root),
            cache=LocalArrayCache(cache_root) if cache_root is not None else None,
        )
        return LocalExecutor(operator_registry).execute(
            plan, requests, context, output_path=output_path,
            checkpoint_path=checkpoint_path, resume_from=resume_from,
        )
    with tempfile.TemporaryDirectory(prefix="pixelprobe-execution-") as directory:
        context = LocalExecutionContext(
            resources, Path(directory),
            cache=LocalArrayCache(cache_root) if cache_root is not None else None,
        )
        return LocalExecutor(operator_registry).execute(
            plan, requests, context, output_path=output_path,
            checkpoint_path=checkpoint_path, resume_from=resume_from,
        )
