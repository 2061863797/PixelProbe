"""V0.9 分块、halo、checkpoint、取消和缓存验收测试。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import RED_Y
from pixelprobe import core
from pixelprobe.engine import (
    CacheKeyInput,
    CheckpointRecord,
    LocalArrayCache,
    LocalExecutionContext,
    NpyArtifactSink,
    choose_chunk_shape,
    encoded_state,
    iter_tensor_chunks,
)
from pixelprobe.engine.errors import (
    CheckpointIncompatibleError,
    ExecutionCancelledError,
    ExecutionTimeoutError,
)
from pixelprobe.operators.base import HaloSpec, ResourcePolicy


def test_chunks_with_halo_merge_without_duplicates_or_gaps(
    test_video: Path, tmp_path: Path,
) -> None:
    tensor = core.create_xt_slice(test_video, RED_Y).tensor
    chunks = tuple(iter_tensor_chunks(
        tensor,
        (2, 11, 3),
        halos=(HaloSpec(before=1), HaloSpec(before=2, after=2), HaloSpec()),
    ))
    assert chunks[0].read_selection[0] == slice(0, 2)
    later = next(chunk for chunk in chunks if chunk.core_selection[0].start == 2)
    assert later.read_selection[0].start == 1
    sink = NpyArtifactSink(
        tmp_path / "merged.npy",
        tensor.data.shape,
        tensor.data.dtype,
        expected_chunks=len(chunks),
    )
    for chunk in chunks:
        sink.write(chunk)
    merged = sink.finalize()
    try:
        assert np.array_equal(merged.materialize(), tensor.data.materialize())
    finally:
        merged.close()


def test_chunk_shape_respects_byte_budget_without_scaling() -> None:
    shape = (120, 1080, 1920, 3)
    chunk = choose_chunk_shape(shape, "uint8", 8 * 1024 * 1024)
    assert len(chunk) == len(shape)
    assert all(1 <= selected <= original for selected, original in zip(chunk, shape))
    assert int(np.prod(chunk, dtype=np.int64)) <= 8 * 1024 * 1024


def test_cancellation_and_timeout_are_stable(tmp_path: Path) -> None:
    cancelled = LocalExecutionContext(
        ResourcePolicy(max_memory_bytes=1024), tmp_path / "cancelled",
    )
    cancelled.cancellation.cancel()
    with pytest.raises(ExecutionCancelledError):
        cancelled.report_progress("node", 0, 1)

    timed_out = LocalExecutionContext(
        ResourcePolicy(max_memory_bytes=1024, timeout_seconds=0.1),
        tmp_path / "timeout",
    )
    timed_out._started -= 1.0
    with pytest.raises(ExecutionTimeoutError):
        timed_out.ensure_active()


def test_checkpoint_requires_exact_execution_identity(tmp_path: Path) -> None:
    context = LocalExecutionContext(
        ResourcePolicy(max_memory_bytes=1024), tmp_path / "execution",
    )
    record = CheckpointRecord(
        plan_id="plan_1",
        request_sha256="1" * 64,
        input_sha256="2" * 64,
        operator_versions={"sample": "1.0.0"},
        completed_chunks=((0,), (1,)),
        state_base64=encoded_state(b"state"),
    )
    path = context.checkpoint("sample", record)
    restored, state = context.load_checkpoint(
        path,
        plan_id="plan_1",
        request_sha256="1" * 64,
        input_sha256="2" * 64,
        operator_versions={"sample": "1.0.0"},
    )
    assert restored.completed_chunks == ((0,), (1,))
    assert state == b"state"
    with pytest.raises(CheckpointIncompatibleError):
        context.load_checkpoint(
            path,
            plan_id="plan_1",
            request_sha256="1" * 64,
            input_sha256="3" * 64,
            operator_versions={"sample": "1.0.0"},
        )


def _cache_key(version: str = "1.0.0") -> CacheKeyInput:
    return CacheKeyInput(
        input_content_sha256="a" * 64,
        operator_name="sample.path_t",
        operator_version=version,
        canonical_config={"interpolation": "nearest"},
        input_tensor_descriptors=({"shape": [4, 8, 3]},),
        dtype="uint8",
        precision="decoded",
        execution_semantics_version="0.1.0",
        artifact_role="data",
    )


def test_cache_invalidates_and_quarantines_corruption(tmp_path: Path) -> None:
    cache = LocalArrayCache(tmp_path / "cache")
    source = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
    written = cache.put(_cache_key(), source)
    written.close()
    hit = cache.get(_cache_key())
    assert hit.handle is not None
    assert np.array_equal(hit.handle.materialize(), source)
    hit.handle.close()
    assert cache.get(_cache_key("1.0.1")).handle is None

    data_path = cache.entries / _cache_key().key() / "data.npy"
    with data_path.open("ab") as handle:
        handle.write(b"corrupt")
    corrupt = cache.get(_cache_key())
    assert corrupt.handle is None and corrupt.warning is not None
    warnings = cache.pop_warnings()
    assert len(warnings) == 1
    assert warnings[0].code == "CACHE_ENTRY_CORRUPT"
    assert any(cache.quarantine.iterdir())


def test_cache_restores_complete_tensor_metadata(
    test_video: Path, tmp_path: Path,
) -> None:
    original = core.create_xt_slice(test_video, RED_Y).tensor
    cache = LocalArrayCache(tmp_path / "tensor-cache")
    cached = cache.put_tensor(_cache_key(), original)
    cached.data.close()  # type: ignore[attr-defined]

    restored = cache.get_tensor(_cache_key())
    assert restored is not None
    try:
        assert np.array_equal(
            restored.data.materialize(), original.data.materialize(),
        )
        assert restored.axes == original.axes
        assert restored.channels == original.channels
        assert restored.coordinate_space == original.coordinate_space
        assert restored.axis_mappings == original.axis_mappings
        assert restored.accuracy == original.accuracy
        assert restored.provenance == original.provenance
        assert restored.attributes == original.attributes
    finally:
        restored.data.close()  # type: ignore[attr-defined]
