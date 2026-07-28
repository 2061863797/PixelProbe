"""不可变类型化 DAG、稳定节点身份与公共子表达式消除。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pixelprobe.engine.errors import ResourcePlanUnsatisfiableError
from pixelprobe.engine.request import RepresentationRequest

EXECUTION_SEMANTICS_VERSION = "1.0.0"
NodeType = Literal[
    "source", "decode", "transform", "sample", "reduce",
    "frequency", "preview", "artifact",
]


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(slots=True, frozen=True)
class GraphNode:
    node_id: str
    node_type: NodeType
    operator_name: str
    operator_version: str
    config_json: str
    input_node_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class ComputationGraph:
    nodes: tuple[GraphNode, ...]
    outputs: tuple[tuple[str, ...], ...]

    def node(self, node_id: str) -> GraphNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)

    def topological(self) -> tuple[GraphNode, ...]:
        known = {node.node_id: node for node in self.nodes}
        visiting: set[str] = set()
        visited: set[str] = set()
        ordered: list[GraphNode] = []

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise ResourcePlanUnsatisfiableError("DAG 包含环")
            if node_id not in known:
                raise ResourcePlanUnsatisfiableError(f"DAG 缺少输入节点：{node_id}")
            visiting.add(node_id)
            for input_id in known[node_id].input_node_ids:
                visit(input_id)
            visiting.remove(node_id)
            visited.add(node_id)
            ordered.append(known[node_id])

        for node in self.nodes:
            visit(node.node_id)
        return tuple(ordered)


def _node(
    node_type: NodeType,
    operator_name: str,
    operator_version: str,
    config: object,
    inputs: tuple[str, ...] = (),
) -> GraphNode:
    config_json = canonical_json(config)
    payload = "\x00".join((
        node_type, operator_name, operator_version, config_json,
        *inputs, EXECUTION_SEMANTICS_VERSION,
    )).encode("utf-8")
    node_id = "n_" + hashlib.sha256(payload).hexdigest()[:24]
    return GraphNode(
        node_id, node_type, operator_name, operator_version, config_json, inputs,
    )


class GraphBuilder:
    def build(
        self, requests: tuple[RepresentationRequest, ...],
    ) -> ComputationGraph:
        if not requests:
            raise ValueError("至少需要一个 RepresentationRequest")
        nodes: dict[str, GraphNode] = {}
        outputs: list[tuple[str, ...]] = []

        def add(node: GraphNode) -> GraphNode:
            nodes.setdefault(node.node_id, node)
            return nodes[node.node_id]

        for request in requests:
            source = add(_node(
                "source", "media.source", "1.0.0",
                request.source.model_dump(mode="json"),
            ))
            decode = add(_node(
                "decode", "media.decode.rgb24", "1.0.0",
                {"sample_semantics": "decoded_sample", "presentation_order": True},
                (source.node_id,),
            ))
            if request.representation in {"frames", "feature_t"}:
                current = add(_node(
                    "sample", "sample.frames", "1.0.0",
                    {
                        "representation": "frames",
                        "selection": request.selection.model_dump(mode="json"),
                        "geometry": None,
                        "feature_config": {},
                    },
                    (decode.node_id,),
                ))
            else:
                current = add(_node(
                    "sample", f"sample.{request.representation}", "1.0.0",
                    {
                        "representation": request.representation,
                        "selection": request.selection.model_dump(mode="json"),
                        "geometry": (
                            request.geometry.model_dump(mode="json")
                            if request.geometry is not None else None
                        ),
                        "feature_config": request.feature.config,
                        "sampling_reduction": (
                            request.reduction.model_dump(mode="json")
                            if request.representation == "roi_t" and request.reduction is not None
                            else None
                        ),
                    },
                    (decode.node_id,),
                ))
            if request.feature.name != "rgb":
                node_type = (
                    "frequency"
                    if request.feature.name in {"temporal_fft", "spatial_fft", "stft"}
                    else "transform"
                )
                current = add(_node(
                    node_type, f"feature.{request.feature.name}", "1.0.0",
                    request.feature.model_dump(mode="json"), (current.node_id,),
                ))
            if request.reduction is not None and request.representation != "roi_t":
                current = add(_node(
                    "reduce", f"reduce.{request.reduction.name}", "1.0.0",
                    request.reduction.model_dump(mode="json"), (current.node_id,),
                ))
            data = add(_node(
                "artifact", "artifact.data", "1.0.0",
                {"format": request.output.format, "role": "data"},
                (current.node_id,),
            ))
            request_outputs = [data.node_id]
            if request.output.include_preview:
                preview_configs = (
                    (
                        {"mode": "flow_direction"},
                        {"mode": "flow_magnitude"},
                    )
                    if request.feature.name in {"flow", "farneback"}
                    else (
                        ({"mode": request.feature.name},)
                        if request.feature.name in {"temporal_fft", "spatial_fft", "stft"}
                        else (request.output.preview_config,)
                    )
                )
                for preview_config in preview_configs:
                    preview = add(_node(
                        "preview", "preview.default", "1.0.0",
                        preview_config, (current.node_id,),
                    ))
                    preview_artifact = add(_node(
                        "artifact", "artifact.preview", "1.0.0",
                        {"format": request.output.format, "role": "preview"},
                        (preview.node_id,),
                    ))
                    request_outputs.append(preview_artifact.node_id)
            outputs.append(tuple(request_outputs))
        graph = ComputationGraph(tuple(nodes.values()), tuple(outputs))
        graph.topological()
        return graph
