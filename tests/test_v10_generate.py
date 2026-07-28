"""V1.0 类型化 DAG、共享解码与统一 generate API 验收测试。"""

from __future__ import annotations

from pathlib import Path
import base64
import hashlib
import json

import numpy as np
import pytest
from PIL import Image

import pixelprobe
from conftest import FRAME_COUNT, RED_Y, make_frame, run_json
from pixelprobe import core
from pixelprobe.domain.geometry import PathGeometry
from pixelprobe.domain.errors import MaterializationLimitExceededError
from pixelprobe.domain.media import MediaSource
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.models.errors import InvalidRangeError
from pixelprobe.engine import (
    FeatureRequest,
    GraphBuilder,
    OutputRequest,
    ReductionRequest,
    RepresentationRequest,
)
from pixelprobe.engine.planner import ExecutionPlan
from pixelprobe.engine.executor import LocalExecutor
from pixelprobe.domain.errors import MediaChangedDuringAnalysisError
from pixelprobe.engine.errors import (
    CheckpointIncompatibleError,
    ExecutionCancelledError,
    OperatorNotRegisteredError,
    ResourcePlanUnsatisfiableError,
)
from pixelprobe.engine.execution import LocalExecutionContext
from pixelprobe.engine.frame_store import SharedFrameStore
from pixelprobe.engine.operator_registry import BoundOperator, OperatorRegistry
from pixelprobe.operators.base import OperatorSpec, ResourcePolicy
from pixelprobe.compat.legacy_requests import (
    legacy_flow_request,
    legacy_reduce_request,
)


def _source(path: Path) -> MediaSource:
    return MediaSource(source_id="source_main", kind="file", uri=str(path))


def _memory() -> OutputRequest:
    return OutputRequest(format="memory", include_preview=False)


def _xt(path: Path, *, output: OutputRequest | None = None) -> RepresentationRequest:
    return RepresentationRequest(
        source=_source(path),
        selection=TemporalSelection(mode="all"),
        representation="xt",
        geometry=PathGeometry(
            type="line", coordinate_space_id="storage_pixels",
            points=((0.0, float(RED_Y)), (31.0, float(RED_Y))),
        ),
        output=output or _memory(),
    )


def test_graph_cse_and_plan_are_stable_and_serializable(test_video: Path) -> None:
    requests = (_xt(test_video), _xt(test_video))
    graph = GraphBuilder().build(requests)
    assert sum(node.node_type == "source" for node in graph.nodes) == 1
    assert sum(node.node_type == "decode" for node in graph.nodes) == 1
    assert graph.outputs[0] == graph.outputs[1]
    plan = pixelprobe.explain(requests)
    assert plan.decode_node_count == 1
    assert ExecutionPlan.model_validate_json(plan.model_dump_json()) == plan
    assert pixelprobe.explain(requests).plan_id == plan.plan_id
    sample = next(
        node for node in plan.nodes if node.operator_name == "sample.xt"
    )
    output = sample.operator_plan.output_descriptors[0]
    assert output.dtype == "uint8"
    assert output.shape == (None, None, 3)
    assert tuple(axis.name for axis in output.axes) == ("time", "x", "channel")
    assert sample.operator_plan.chunk_axes == ("time", "x", "channel")


def test_multiple_requests_merge_every_resource_limit_strictly(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    loose = _xt(test_video).model_copy(update={
        "resources": ResourcePolicy(
            max_memory_bytes=512 * 1024 * 1024,
            max_temporary_bytes=64 * 1024 * 1024,
            timeout_seconds=90.0,
            preferred_chunk_bytes=32 * 1024 * 1024,
            allow_partial=True,
        ),
    })
    strict = _xt(test_video).model_copy(update={
        "resources": ResourcePolicy(
            max_memory_bytes=128 * 1024 * 1024,
            max_temporary_bytes=8 * 1024 * 1024,
            timeout_seconds=10.0,
            preferred_chunk_bytes=4 * 1024 * 1024,
            allow_partial=False,
        ),
    })
    expected = ResourcePolicy(
        max_memory_bytes=128 * 1024 * 1024,
        max_temporary_bytes=8 * 1024 * 1024,
        timeout_seconds=10.0,
        preferred_chunk_bytes=4 * 1024 * 1024,
        allow_partial=False,
    )
    plan = pixelprobe.explain((loose, strict))
    assert plan.resources == expected
    changed_timeout = strict.model_copy(update={
        "resources": strict.resources.model_copy(update={"timeout_seconds": 11.0}),
    })
    assert pixelprobe.explain((loose, changed_timeout)).plan_id != plan.plan_id

    captured: dict[str, ResourcePolicy] = {}
    sentinel = object()

    def capture_execute(self, plan_arg, requests_arg, context, **kwargs):
        captured["resources"] = context.resources
        return sentinel

    monkeypatch.setattr(LocalExecutor, "execute", capture_execute)
    assert pixelprobe.generate(
        (loose, strict), temporary_root=tmp_path / "execution-temp",
    ) is sentinel
    assert captured["resources"] == expected


def test_explain_rejects_unregistered_feature_before_execution(
    test_video: Path,
) -> None:
    request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(mode="all"),
        representation="feature_t",
        feature=FeatureRequest(name="not_registered"),
        output=_memory(),
    )
    with pytest.raises(OperatorNotRegisteredError):
        pixelprobe.explain(request)


def test_dag_keeps_distinct_time_selections_after_shared_decode(
    test_video: Path,
) -> None:
    requests = tuple(
        RepresentationRequest(
            source=_source(test_video),
            selection=TemporalSelection(
                mode="indices", requested_indices=(index,),
            ),
            representation="frames",
            output=_memory(),
        )
        for index in (0, 1)
    )
    graph = GraphBuilder().build(requests)
    assert sum(node.node_type == "decode" for node in graph.nodes) == 1
    assert sum(node.operator_name == "sample.frames" for node in graph.nodes) == 2
    assert graph.outputs[0] != graph.outputs[1]

    result = pixelprobe.generate(requests)
    first, second = (group[0] for group in result.request_tensors)
    assert result.decode_passes == 1
    assert first.attributes["presentation_indices"] == [0]
    assert second.attributes["presentation_indices"] == [1]
    assert not np.array_equal(first.data.materialize(), second.data.materialize())


def test_bundle_keeps_same_named_tensors_from_distinct_dag_nodes(
    test_video: Path, tmp_path: Path,
) -> None:
    requests = tuple(
        RepresentationRequest(
            source=_source(test_video),
            selection=TemporalSelection(mode="indices", requested_indices=(index,)),
            representation="frames",
            output=OutputRequest(format="bundle", include_preview=False),
        )
        for index in (0, 1)
    )

    result = pixelprobe.generate(
        requests, output_path=tmp_path / "distinct.bundle",
    )

    assert result.bundle is not None
    records = tuple(
        item for item in result.bundle.manifest.artifacts if item.kind == "data"
    )
    assert len(records) == 2
    assert len({item.artifact_id for item in records}) == 2
    assert len({item.attributes["dag_node_id"] for item in records}) == 2
    restored = tuple(
        result.bundle.open_tensor(record.artifact_id) for record in records
    )
    coordinate_refs = tuple(
        tensor.axes[0].coordinates_ref for tensor in restored
    )
    assert all(reference is not None for reference in coordinate_refs)
    assert len({reference.artifact_id for reference in coordinate_refs if reference}) == 2
    assert set(records[0].mapping_ids).isdisjoint(records[1].mapping_ids)
    assert np.array_equal(
        result.request_tensors[0][0].data.materialize()[0],
        core.get_frame(test_video, frame=0)[0],
    )
    assert np.array_equal(
        result.request_tensors[1][0].data.materialize()[0],
        core.get_frame(test_video, frame=1)[0],
    )
    for tensor in restored:
        tensor.data.close()  # type: ignore[attr-defined]


def test_generate_cache_hit_restores_tensor_without_decoding(
    test_video: Path, tmp_path: Path,
) -> None:
    request = _xt(test_video)
    cache_root = tmp_path / "cache"
    first = pixelprobe.generate(request, cache_root=cache_root)
    assert first.decode_passes == 1
    assert first.cache_hits == 0
    assert first.cache_writes == 1

    second = pixelprobe.generate(request, cache_root=cache_root)
    assert second.decode_passes == 0
    assert second.cache_hits == 1
    assert second.cache_writes == 0
    assert np.array_equal(
        first.request_tensors[0][0].data.materialize(),
        second.request_tensors[0][0].data.materialize(),
    )
    assert first.request_tensors[0][0].axes == second.request_tensors[0][0].axes
    assert (
        first.request_tensors[0][0].axis_mappings
        == second.request_tensors[0][0].axis_mappings
    )


def test_generate_runs_registered_operator_execute_and_finalize(
    test_video: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    original_execute = BoundOperator.execute
    original_finalize = BoundOperator.finalize

    def record_execute(self, *args, **kwargs):
        calls.append(("execute", self.spec.name))
        return original_execute(self, *args, **kwargs)

    def record_finalize(self, *args, **kwargs):
        calls.append(("finalize", self.spec.name))
        return original_finalize(self, *args, **kwargs)

    monkeypatch.setattr(BoundOperator, "execute", record_execute)
    monkeypatch.setattr(BoundOperator, "finalize", record_finalize)
    pixelprobe.generate(_xt(test_video))

    assert calls == [
        ("execute", "sample.xt"),
        ("finalize", "sample.xt"),
    ]


def test_generate_executes_custom_registered_runtime_without_executor_branch(
    test_video: Path, tmp_path: Path,
) -> None:
    registry = OperatorRegistry()
    calls: list[str] = []
    consumed_chunks: list[tuple[int, ...]] = []

    def infer_identity(inputs, config):
        del config
        return inputs

    def execute_identity(invocation):
        calls.append(invocation.operator_name)
        consumed_chunks.extend(
            chunk.chunk_index for chunk in invocation.input_chunks[0]
        )
        value = invocation.inputs[0]
        assert isinstance(value, tuple)
        return value

    registry.register(
        "feature.identity",
        spec=OperatorSpec(
            name="feature.identity",
            version="1.0.0",
            category="transform",
            deterministic="bit_exact",
            stateful=False,
            chunkable=True,
            cacheable=True,
            supported_dtypes=("uint8",),
            config_schema_id="pixelprobe.test.identity.v1",
        ),
        validator=FeatureRequest.model_validate,
        infer=infer_identity,
        runtime=execute_identity,
    )
    request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(mode="indices", requested_indices=(0, 1)),
        representation="feature_t",
        feature=FeatureRequest(name="identity"),
        output=_memory(),
        resources=ResourcePolicy(
            max_memory_bytes=268_435_456,
            preferred_chunk_bytes=1_024,
        ),
    )

    cache_root = tmp_path / "custom-cache"
    result = pixelprobe.generate(
        request, registry=registry, cache_root=cache_root,
    )
    cached = pixelprobe.generate(
        request, registry=registry, cache_root=cache_root,
    )

    assert calls == ["feature.identity"]
    assert consumed_chunks == [(0, 0), (0, 1)]
    assert cached.decode_passes == 0
    assert cached.cache_hits == 2
    assert np.array_equal(
        result.request_tensors[0][0].data.materialize()[0],
        core.get_frame(test_video, frame=0)[0],
    )


def test_generate_frame_difference_preserves_exact_pairs_and_halo(
    test_video: Path,
) -> None:
    request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(
            mode="indices", requested_indices=(0, 1, 2),
        ),
        representation="feature_t",
        feature=FeatureRequest(name="frame_difference"),
        output=_memory(),
    )

    result = pixelprobe.generate(request)
    tensor = result.request_tensors[0][0]
    actual = tensor.data.materialize()
    frame_0 = core.get_frame(test_video, frame=0)[0].astype(np.int16)
    frame_1 = core.get_frame(test_video, frame=1)[0].astype(np.int16)

    assert actual.shape == (2, *frame_0.shape)
    assert actual.dtype == np.uint8
    assert np.array_equal(actual[0], np.abs(frame_1 - frame_0).astype(np.uint8))
    assert tensor.attributes["presentation_indices"] == [1, 2]
    assert tensor.attributes["frame_pairs"] == [[0, 1], [1, 2]]
    assert tensor.axis_mappings[0].parameters["frame_pairs"] == [[0, 1], [1, 2]]
    graph = GraphBuilder().build((request,))
    diff_node = next(node for node in graph.nodes if node.operator_name.endswith("frame_difference"))
    operator = OperatorRegistry().resolve(diff_node)
    assert operator.spec.temporal_halo.before == 1


def test_generate_stft_returns_complex_data_and_preview(test_video: Path) -> None:
    request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(mode="all"),
        representation="feature_t",
        feature=FeatureRequest(name="stft", config={
            "source": "luma",
            "window": "hann",
            "length": 8,
            "hop": 4,
            "padding": "none",
            "normalization": "window_energy",
        }),
        output=OutputRequest(format="memory", include_preview=True),
    )

    result = pixelprobe.generate(request)
    data, preview = result.request_tensors[0]

    assert data.data.dtype == np.dtype("complex128")
    assert data.data.shape == (6, 5)
    assert tuple(axis.name for axis in data.axes) == ("window_time", "frequency")
    assert data.axes[1].unit == "hertz"
    assert data.attributes["window"] == "hann"
    assert data.attributes["hop"] == 4
    assert preview.attributes["artifact_role"] == "preview"
    assert preview.data.shape == (5, 6, 3)


def test_generate_temporal_fft_requires_explicit_vfr_policy(
    vfr_video: Path,
) -> None:
    base = RepresentationRequest(
        source=_source(vfr_video),
        selection=TemporalSelection(mode="all"),
        representation="feature_t",
        feature=FeatureRequest(name="temporal_fft", config={"source": "luma"}),
        output=_memory(),
    )

    with pytest.raises(InvalidRangeError, match="VFR"):
        pixelprobe.generate(base)

    compatibility = base.model_copy(update={
        "feature": FeatureRequest(name="temporal_fft", config={
            "source": "luma", "vfr_policy": "estimate",
        }),
    })
    tensor = pixelprobe.generate(compatibility).request_tensors[0][0]
    assert tensor.accuracy.level.value == "estimated"
    assert tensor.attributes["vfr_compatibility_estimate"] is True


def test_generate_enforces_framestore_temporary_budget(test_video: Path) -> None:
    request = _xt(test_video).model_copy(update={
        "resources": ResourcePolicy(
            max_memory_bytes=268_435_456,
            max_temporary_bytes=32 * 24 * 3 - 1,
        ),
    })

    with pytest.raises(ResourcePlanUnsatisfiableError):
        pixelprobe.generate(request)


@pytest.mark.parametrize("color_model", ["grayscale", "hsv", "lab"])
def test_generate_color_conversion_keeps_data_separate_from_preview(
    test_video: Path, color_model: str,
) -> None:
    request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(mode="indices", requested_indices=(0,)),
        representation="feature_t",
        feature=FeatureRequest(name=color_model),
        output=OutputRequest(format="memory", include_preview=True),
    )

    result = pixelprobe.generate(request)
    data, preview = result.request_tensors[0]
    decoded = core.get_frame(test_video, frame=0)[0]

    assert data.data.dtype == np.dtype("float32")
    assert data.attributes["artifact_role"] == "data"
    assert data.attributes["color_model"] == color_model
    assert preview.attributes["artifact_role"] == "preview"
    assert preview.data.shape == decoded.shape
    if color_model == "grayscale":
        expected = np.tensordot(
            decoded.astype(np.float32),
            np.asarray((0.299, 0.587, 0.114), dtype=np.float32),
            axes=([-1], [0]),
        )
        assert data.data.shape == (1, *decoded.shape[:2])
        assert np.allclose(data.data.materialize()[0], expected, atol=1e-5)
    else:
        assert data.data.shape == (1, *decoded.shape)
        restored = preview.data.materialize()
        assert np.max(np.abs(restored.astype(np.int16) - decoded.astype(np.int16))) <= 1


def test_generate_surfaces_corrupt_cache_as_warning(
    test_video: Path, tmp_path: Path,
) -> None:
    cache_root = tmp_path / "cache"
    first = pixelprobe.generate(_xt(test_video), cache_root=cache_root)
    for tensor in first.tensors:
        tensor.data.close()  # type: ignore[attr-defined]
    entry = next((cache_root / "entries").iterdir())
    with (entry / "data.npy").open("ab") as output:
        output.write(b"corrupt")

    second = pixelprobe.generate(_xt(test_video), cache_root=cache_root)

    assert any(
        event.get("code") == "CACHE_ENTRY_CORRUPT"
        for event in second.events
    )
    assert second.decode_passes == 1
    assert second.cache_hits == 0
    assert second.cache_writes == 1


def test_generate_resume_requires_matching_plan_input_and_cache(
    test_video: Path, tmp_path: Path,
) -> None:
    request = _xt(test_video)
    cache_root = tmp_path / "cache"
    checkpoint = tmp_path / "generate.checkpoint.json"
    first = pixelprobe.generate(
        request, cache_root=cache_root, checkpoint_path=checkpoint,
    )
    assert first.cache_writes == 1 and checkpoint.is_file()

    resumed = pixelprobe.generate(
        request, cache_root=cache_root, resume_from=checkpoint,
    )
    assert resumed.decode_passes == 0
    assert resumed.cache_hits == 1

    changed = request.model_copy(update={
        "selection": TemporalSelection(
            mode="indices", requested_indices=(0,),
        ),
    })
    with pytest.raises(CheckpointIncompatibleError):
        pixelprobe.generate(
            changed, cache_root=cache_root, resume_from=checkpoint,
        )


def test_generate_resumes_completed_sampling_chunks_after_interruption(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _xt(test_video)
    cache_root = tmp_path / "cache"
    checkpoint = tmp_path / "chunk.checkpoint.json"
    original = LocalExecutionContext.checkpoint_to

    def interrupt_after_two_chunks(self, target, record):
        result = original(self, target, record)
        if len(record.completed_chunks) == 2:
            raise ExecutionCancelledError("测试中断")
        return result

    monkeypatch.setattr(LocalExecutionContext, "checkpoint_to", interrupt_after_two_chunks)
    with pytest.raises(ExecutionCancelledError):
        pixelprobe.generate(
            request,
            cache_root=cache_root,
            checkpoint_path=checkpoint,
        )
    record = json.loads(checkpoint.read_text(encoding="utf-8"))
    state = json.loads(base64.b64decode(record["state_base64"]))
    sample_chunks = next(iter(state["completed_chunks"].values()))
    assert sample_chunks == [0, 1]
    assert any((cache_root / "partials").rglob("data.npy"))

    monkeypatch.setattr(LocalExecutionContext, "checkpoint_to", original)
    resumed = pixelprobe.generate(
        request,
        cache_root=cache_root,
        resume_from=checkpoint,
    )
    assert resumed.cache_writes == 1
    assert not any((cache_root / "partials").rglob("data.npy"))
    assert np.array_equal(
        resumed.request_tensors[0][0].data.materialize(),
        core.create_xt_slice(test_video, RED_Y).array,
    )


def test_generate_resumes_frame_tensor_chunks_before_reduction(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = legacy_reduce_request(
        test_video, operation="mean", rect=None,
    )
    cache_root = tmp_path / "cache"
    checkpoint = tmp_path / "frames.checkpoint.json"
    original = LocalExecutionContext.checkpoint_to

    def interrupt_after_two_chunks(self, target, record):
        result = original(self, target, record)
        if len(record.completed_chunks) == 2:
            raise ExecutionCancelledError("模拟帧 Tensor 分块中断")
        return result

    monkeypatch.setattr(LocalExecutionContext, "checkpoint_to", interrupt_after_two_chunks)
    with pytest.raises(ExecutionCancelledError):
        pixelprobe.generate(
            request, cache_root=cache_root, checkpoint_path=checkpoint,
        )
    monkeypatch.setattr(LocalExecutionContext, "checkpoint_to", original)

    resumed = pixelprobe.generate(
        request, cache_root=cache_root, resume_from=checkpoint,
    )
    expected = core.temporal_reduce(test_video, op="mean")
    assert np.array_equal(
        resumed.request_tensors[0][0].data.materialize(),
        expected.data_tensor.data.materialize(),
    )


def test_generate_resumes_reduction_input_chunks(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = legacy_reduce_request(test_video, operation="mean", rect=None)
    cache_root = tmp_path / "cache"
    checkpoint = tmp_path / "reduction.checkpoint.json"
    original = LocalExecutionContext.checkpoint_to

    def interrupt_inside_reduction(self, target, record):
        result = original(self, target, record)
        state = json.loads(base64.b64decode(record.state_base64))
        chunk_groups = list(state["completed_chunks"].values())
        if len(chunk_groups) >= 2 and any(
            len(group) == 2 for group in chunk_groups
        ):
            raise ExecutionCancelledError("模拟 Reduction 输入分块中断")
        return result

    monkeypatch.setattr(
        LocalExecutionContext, "checkpoint_to", interrupt_inside_reduction,
    )
    with pytest.raises(ExecutionCancelledError):
        pixelprobe.generate(
            request, cache_root=cache_root, checkpoint_path=checkpoint,
        )
    state = json.loads(base64.b64decode(
        json.loads(checkpoint.read_text("utf-8"))["state_base64"],
    ))
    assert len(state["completed_chunks"]) == 2
    assert any(
        len(group) == 2 for group in state["completed_chunks"].values()
    )

    monkeypatch.setattr(LocalExecutionContext, "checkpoint_to", original)
    resumed = pixelprobe.generate(
        request, cache_root=cache_root, resume_from=checkpoint,
    )
    expected = core.temporal_reduce(test_video, op="mean")
    assert np.array_equal(
        resumed.request_tensors[0][0].data.materialize(),
        expected.data_tensor.data.materialize(),
    )
    assert not any((cache_root / "partials").rglob("data.npy"))


def test_generate_shares_one_decode_across_xt_path_reduce_and_flow(
    test_video: Path,
) -> None:
    expected_xt = core.create_xt_slice(test_video, RED_Y).array
    path_request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(mode="all"),
        representation="path_t",
        geometry=PathGeometry(
            type="line", coordinate_space_id="storage_pixels",
            points=((0.0, float(RED_Y)), (31.0, float(RED_Y))),
        ),
        feature=FeatureRequest(
            name="rgb", config={"sample_count": 32, "interpolation": "nearest"},
        ),
        output=_memory(),
    )
    reduction_request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(mode="all"),
        representation="frames",
        reduction=ReductionRequest(name="mean", axes=("time",)),
        output=_memory(),
    )
    flow_request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(mode="indices", requested_indices=(0, 1)),
        representation="feature_t",
        feature=FeatureRequest(name="farneback"),
        output=_memory(),
    )
    result = pixelprobe.generate(
        (_xt(test_video), path_request, reduction_request, flow_request)
    )
    assert result.decode_passes == 1
    assert result.plan.decode_node_count == 1
    assert np.array_equal(
        result.request_tensors[0][0].data.materialize(), expected_xt,
    )
    assert np.array_equal(
        result.request_tensors[1][0].data.materialize(), expected_xt,
    )
    reduction = result.request_tensors[2][0].data.materialize()
    assert reduction.shape == (32, 32, 3) and reduction.dtype == np.float64
    flow = result.request_tensors[3][0].data.materialize()
    assert flow.shape == (32, 32, 2) and flow.dtype == np.float32
    assert len(result.events) == 4


def test_unified_reduce_preserves_legacy_diff_rect_and_preview(
    test_video: Path,
) -> None:
    rect = (3, 4, 12, 10)
    legacy = core.temporal_reduce(
        test_video,
        op="diff",
        rect=rect,
        p_low=5.0,
        p_high=95.0,
        destripe=True,
        smooth=3,
    )
    request = legacy_reduce_request(
        test_video,
        operation="diff",
        rect=rect,
        p_low=5.0,
        p_high=95.0,
        destripe=True,
        smooth=3,
    )
    result = pixelprobe.generate(request)
    data, preview = result.request_tensors[0]
    assert np.array_equal(data.data.materialize(), legacy.data_tensor.data.materialize())
    assert np.array_equal(preview.data.materialize(), legacy.image)
    assert preview.attributes["stretch_low_value"] == legacy.stretch_low_value
    assert preview.attributes["stretch_high_value"] == legacy.stretch_high_value


def test_unified_reduce_supports_exact_rms_and_percentile(test_video: Path) -> None:
    base = {
        "source": _source(test_video),
        "selection": TemporalSelection(mode="all"),
        "representation": "frames",
        "output": _memory(),
    }
    rms_request = RepresentationRequest(
        **base,
        reduction=ReductionRequest(name="rms", axes=("time",)),
    )
    percentile_request = RepresentationRequest(
        **base,
        reduction=ReductionRequest(
            name="percentile", axes=("time",), config={"percentile": 25.0},
        ),
    )

    result = pixelprobe.generate((rms_request, percentile_request))
    frames = np.stack([make_frame(index) for index in range(FRAME_COUNT)]).astype(np.float64)

    assert np.allclose(
        result.request_tensors[0][0].data.materialize(),
        np.sqrt(np.mean(frames * frames, axis=0)),
    )
    assert np.allclose(
        result.request_tensors[1][0].data.materialize(),
        np.percentile(frames, 25.0, axis=0),
    )
    assert result.decode_passes == 1


def test_reduction_request_rejects_unsupported_axes_and_missing_percentile() -> None:
    with pytest.raises(ValueError, match="axes"):
        ReductionRequest(name="mean", axes=("x",))
    with pytest.raises(ValueError, match="percentile"):
        ReductionRequest(name="percentile", axes=("time",))


def test_unified_flow_preserves_legacy_compensation_stats_and_previews(
    motion_video: Path,
) -> None:
    pytest.importorskip("cv2")
    legacy = core.compute_flow(
        motion_video,
        frame_a=0,
        frame_b=4,
        compensate_global=True,
        mag_threshold=0.5,
    )
    request = legacy_flow_request(
        motion_video,
        frame_a=0,
        frame_b=4,
        compensate_global=True,
        mag_threshold=0.5,
    )
    result = pixelprobe.generate(request)
    raw, flow, magnitude, flow_preview, magnitude_preview = result.request_tensors[0]
    assert np.array_equal(raw.data.materialize(), legacy.raw_flow_tensor.data.materialize())
    assert np.array_equal(flow.data.materialize(), legacy.flow_tensor.data.materialize())
    assert np.array_equal(magnitude.data.materialize(), legacy.magnitude_tensor.data.materialize())
    assert np.array_equal(flow_preview.data.materialize(), legacy.flow_image)
    assert np.array_equal(magnitude_preview.data.materialize(), legacy.magnitude_image)
    assert magnitude.attributes["global_motion"] == legacy.global_motion
    assert magnitude.attributes["motion_bbox"] == legacy.motion_bbox


def test_generate_bundle_records_request_and_exact_data(
    test_video: Path, tmp_path: Path,
) -> None:
    request = _xt(
        test_video, output=OutputRequest(format="bundle", include_preview=False),
    )
    target = tmp_path / "generated.bundle"
    result = pixelprobe.generate(request, output_path=target)
    assert result.bundle is not None
    stored_request = result.bundle.manifest.requests[0]
    assert stored_request.source == request.source.model_copy(
        update={"uri": "source://source_main"},
    )
    assert stored_request.model_copy(update={"source": request.source}) == request
    assert result.bundle.manifest.sources[0].metadata_policy == "safe"
    assert result.bundle.manifest.sources[0].media_identity.actual_format == "matroska"
    assert result.bundle.manifest.sources[0].original_uri is None
    assert stored_request.source.uri == "source://source_main"
    assert str(test_video.resolve()) not in (
        result.bundle.root / "manifest.json"
    ).read_text(encoding="utf-8")
    # generate() 的内部临时目录此时已清理，返回句柄必须指向 Bundle 持久数据。
    assert np.array_equal(
        result.request_tensors[0][0].data.materialize(),
        core.create_xt_slice(test_video, RED_Y).array,
    )
    data_record = next(
        item for item in result.bundle.manifest.artifacts if item.kind == "data"
    )
    plan_record = next(
        item for item in result.bundle.manifest.artifacts
        if item.role == "execution_plan"
    )
    events_record = next(
        item for item in result.bundle.manifest.artifacts
        if item.role == "execution_events"
    )
    summary = result.bundle.manifest.execution_summary
    assert summary["plan_artifact_id"] == plan_record.artifact_id
    assert summary["events_artifact_id"] == events_record.artifact_id
    assert summary["event_count"] == len(result.events)
    stored_plan = json.loads(
        (result.bundle.root / plan_record.storage.files[0].uri).read_text("utf-8")
    )
    plan_text = (result.bundle.root / plan_record.storage.files[0].uri).read_text("utf-8")
    assert str(test_video.resolve()) not in plan_text
    source_node = next(node for node in stored_plan["nodes"] if node["node_type"] == "source")
    assert json.loads(source_node["config_json"])["uri"] == "source://source_main"
    stored_events = [
        json.loads(line)
        for line in (result.bundle.root / events_record.storage.files[0].uri)
        .read_text("utf-8").splitlines()
    ]
    assert stored_events == list(result.events)
    restored = result.bundle.open_tensor(data_record.artifact_id)
    try:
        assert restored.data.shape == (FRAME_COUNT, 32, 3)
        assert np.array_equal(
            restored.data.materialize(), core.create_xt_slice(test_video, RED_Y).array,
        )
    finally:
        restored.data.close()  # type: ignore[attr-defined]


def test_generate_full_metadata_policy_is_explicit_and_warned(
    test_video: Path, tmp_path: Path,
) -> None:
    request = _xt(
        test_video,
        output=OutputRequest(
            format="bundle", include_preview=False, metadata_policy="full",
        ),
    )
    result = pixelprobe.generate(
        request, output_path=tmp_path / "full-metadata.bundle",
    )
    assert result.bundle is not None
    source = result.bundle.manifest.sources[0]
    assert source.metadata_policy == "full"
    assert source.original_uri == str(test_video.resolve())
    assert result.bundle.manifest.requests[0].source.uri == str(test_video.resolve())
    assert result.bundle.manifest.warnings[0]["code"] == "FULL_METADATA_PRIVACY_RISK"
    plan_record = next(
        item for item in result.bundle.manifest.artifacts
        if item.role == "execution_plan"
    )
    stored_plan = json.loads(
        (result.bundle.root / plan_record.storage.files[0].uri).read_text("utf-8")
    )
    source_node = next(node for node in stored_plan["nodes"] if node["node_type"] == "source")
    assert json.loads(source_node["config_json"])["uri"] == str(test_video.resolve())


def test_generate_rejects_same_source_id_for_different_files(
    test_video: Path, motion_video: Path,
) -> None:
    first = _xt(test_video)
    second = first.model_copy(update={
        "source": _source(motion_video),
    })
    with pytest.raises(InvalidRangeError, match="source_id"):
        pixelprobe.generate((first, second))


def test_source_change_detection_uses_content_not_only_size_or_mtime(
    tmp_path: Path,
) -> None:
    source = tmp_path / "same-stat.bin"
    source.write_bytes(b"abc")
    stat = source.stat()
    expected = hashlib.sha256(b"abc").hexdigest()
    source.write_bytes(b"abd")
    source.touch()
    import os
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    with pytest.raises(MediaChangedDuringAnalysisError):
        LocalExecutor._verify_sources_unchanged({source: expected})


def test_generate_memory_output_fails_loud_when_exact_result_exceeds_budget(
    test_video: Path,
) -> None:
    request = _xt(test_video).model_copy(update={
        "resources": ResourcePolicy(max_memory_bytes=100),
    })
    with pytest.raises(MaterializationLimitExceededError):
        pixelprobe.generate(request)


def test_generate_cli_uses_request_file_and_stable_json(
    test_video: Path, tmp_path: Path,
) -> None:
    request = _xt(test_video, output=OutputRequest(format="memory"))
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
    target = tmp_path / "cli.bundle"
    data = run_json(
        "generate",
        test_video,
        "--request", request_path,
        "--output", target,
        "--json",
    )["data"]
    assert data["path"] == str(target)
    assert data["decode_passes"] == 1
    assert data["artifact_count"] >= 4
    assert (target / "manifest.json").is_file()


def test_generate_image_preserves_all_source_pixels(test_image: Path) -> None:
    request = RepresentationRequest(
        source=_source(test_image),
        selection=TemporalSelection(mode="all"),
        representation="frames",
        output=_memory(),
    )
    result = pixelprobe.generate(request)
    expected, _, _, info = core.load_frame(test_image)
    actual = result.request_tensors[0][0].data.materialize()
    assert result.decode_passes == 1
    assert actual.shape == (1, info.height, info.width, 3)
    assert np.array_equal(actual[0], expected)
    assert result.request_tensors[0][0].attributes[
        "presentation_indices"
    ] == [0]


def test_generate_rejects_frame_interval_outside_actual_presentation_range(
    test_video: Path,
) -> None:
    request = RepresentationRequest(
        source=_source(test_video),
        selection=TemporalSelection(
            mode="frame_interval",
            requested_start_frame=0,
            requested_end_frame_exclusive=FRAME_COUNT + 1,
        ),
        representation="frames",
        output=_memory(),
    )
    with pytest.raises(InvalidRangeError, match="超出实际展示帧范围"):
        pixelprobe.generate(request)


def test_shared_frame_store_keeps_packet_metadata(
    vfr_video: Path, tmp_path: Path,
) -> None:
    context = LocalExecutionContext(
        ResourcePolicy(max_memory_bytes=4 * 1024 * 1024), tmp_path / "execution",
    )
    with SharedFrameStore(vfr_video, context) as store:
        assert len(store.frame_metadata) == len(store.times)
        assert [item.presentation_index for item in store.frame_metadata] == list(
            range(len(store.frame_metadata))
        )
        assert all(item.pts is not None for item in store.frame_metadata)
        assert all(item.time_base is not None for item in store.frame_metadata)
        assert all(
            item.timeline_time_seconds == timestamp
            for item, timestamp in zip(store.frame_metadata, store.times)
        )


def test_unified_engine_preserves_native_rgba_while_marking_display_rgb8(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "transparent.png"
    source = np.array([[[1, 2, 3, 4]]], dtype=np.uint8)
    Image.fromarray(source).save(image_path)

    context = LocalExecutionContext(
        ResourcePolicy(max_memory_bytes=4 * 1024 * 1024), tmp_path / "execution",
    )
    with SharedFrameStore(image_path, context) as store:
        assert store.native_image is not None
        assert store.native_image_metadata is not None
        assert np.array_equal(
            store.native_image.read((slice(None), slice(None), slice(None))),
            source,
        )
        assert store.native_image_metadata.mode == "RGBA"
        assert store.native_image_metadata.dtype == "uint8"
        assert store.native_image_metadata.bits_per_sample == 8
        assert store.native_image_metadata.has_alpha is True
        assert store.native_image_metadata.alpha_representation == "straight"
        assert store.frame_metadata[0].sample_semantics == "display_rgb8"
        assert "DISPLAY_RGB8_CONVERSION" in store.frame_metadata[0].flags
        assert "NATIVE_IMAGE_PRESERVED" in store.frame_metadata[0].flags

    request = RepresentationRequest(
        source=_source(image_path),
        selection=TemporalSelection(mode="all"),
        representation="frames",
        output=_memory(),
    )
    result = pixelprobe.generate(request)
    assert np.array_equal(
        result.tensors[0].data.read((0, slice(None), slice(None), slice(None))),
        source[..., :3],
    )


def test_shared_frame_store_preserves_native_high_bit_image(tmp_path: Path) -> None:
    image_path = tmp_path / "high-bit.png"
    source = np.array([[0, 65535]], dtype=np.uint16)
    Image.fromarray(source).save(image_path)

    context = LocalExecutionContext(
        ResourcePolicy(max_memory_bytes=4 * 1024 * 1024), tmp_path / "execution",
    )
    with SharedFrameStore(image_path, context) as store:
        assert store.native_image is not None
        assert store.native_image_metadata is not None
        assert np.array_equal(
            store.native_image.read((slice(None), slice(None))), source,
        )
        assert store.native_image_metadata.mode == "I;16"
        assert store.native_image_metadata.dtype == "uint16"
        assert store.native_image_metadata.bits_per_sample == 16
        assert store.frame_metadata[0].sample_semantics == "display_rgb8"
        assert "DISPLAY_RGB8_CONVERSION" in store.frame_metadata[0].flags
