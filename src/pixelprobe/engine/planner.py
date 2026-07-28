"""确定性本地 CPU Planner 与可解释 ExecutionPlan。"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from pixelprobe.engine.graph import (
    EXECUTION_SEMANTICS_VERSION,
    ComputationGraph,
    canonical_json,
)
from pixelprobe.engine.request import RepresentationRequest, merge_resource_policies
from pixelprobe.engine.operator_registry import OperatorRegistry
from pixelprobe.operators.base import OperatorPlan, ResourcePolicy, TensorDescriptor


class PlannedNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    node_type: str
    operator_name: str
    operator_version: str
    config_json: str
    input_node_ids: tuple[str, ...]
    preferred_chunk_bytes: int = Field(gt=0)
    checkpoint_boundary: bool
    cacheable: bool
    operator_plan: OperatorPlan


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "0.1.0"
    plan_id: str
    execution_semantics_version: str
    nodes: tuple[PlannedNode, ...]
    outputs: tuple[tuple[str, ...], ...]
    request_count: int
    decode_node_count: int
    estimated_peak_memory_bytes: int = Field(ge=0)
    # 旧 Bundle 的 ExecutionPlan 没有这个字段，读取时保持兼容。
    resources: ResourcePolicy | None = None


class Planner:
    def __init__(self, registry: OperatorRegistry | None = None) -> None:
        self.registry = registry or OperatorRegistry()

    def plan(
        self,
        graph: ComputationGraph,
        requests: tuple[RepresentationRequest, ...],
    ) -> ExecutionPlan:
        ordered = graph.topological()
        resources = merge_resource_policies(
            tuple(request.resources for request in requests)
        )
        preferred = resources.preferred_chunk_bytes
        descriptors: dict[str, tuple[TensorDescriptor, ...]] = {}
        planned_nodes: list[PlannedNode] = []
        for node in ordered:
            operator = self.registry.resolve(node)
            inputs = tuple(
                descriptor
                for input_id in node.input_node_ids
                for descriptor in descriptors[input_id]
            )
            config = operator.validate_config(json.loads(node.config_json))
            outputs = operator.infer_output(inputs, config)
            operator_plan = operator.plan(inputs, outputs, config, resources)
            descriptors[node.node_id] = outputs
            planned_nodes.append(PlannedNode(
                node_id=node.node_id,
                node_type=node.node_type,
                operator_name=node.operator_name,
                operator_version=node.operator_version,
                config_json=canonical_json(config.model_dump(mode="json")),
                input_node_ids=node.input_node_ids,
                preferred_chunk_bytes=preferred,
                checkpoint_boundary=(
                    operator.spec.stateful
                    or node.node_type in {"sample", "reduce", "artifact"}
                ),
                cacheable=operator.spec.cacheable,
                operator_plan=operator_plan,
            ))
        planned = tuple(planned_nodes)
        payload = canonical_json({
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_type": node.node_type,
                    "operator_name": node.operator_name,
                    "operator_version": node.operator_version,
                    "config_json": node.config_json,
                    "input_node_ids": node.input_node_ids,
                }
                for node in ordered
            ],
            "outputs": graph.outputs,
            "semantics": EXECUTION_SEMANTICS_VERSION,
            "resources": resources.model_dump(mode="json"),
        })
        plan_id = "plan_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return ExecutionPlan(
            plan_id=plan_id,
            execution_semantics_version=EXECUTION_SEMANTICS_VERSION,
            nodes=planned,
            outputs=graph.outputs,
            request_count=len(requests),
            decode_node_count=sum(node.node_type == "decode" for node in ordered),
            estimated_peak_memory_bytes=max(
                (node.operator_plan.estimated_peak_memory_bytes for node in planned),
                default=0,
            ),
            resources=resources,
        )
