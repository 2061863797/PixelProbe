"""执行临时文件的合计配额与写入前空间检查。"""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pixelprobe.engine.execution import LocalExecutionContext
from pixelprobe.engine.executor import _rows_to_handle
from pixelprobe.engine.frame_store import SharedFrameStore
from pixelprobe.engine.errors import ResourcePlanUnsatisfiableError
from pixelprobe.operators.base import ResourcePolicy


def context_at(root, limit=1024):
    return LocalExecutionContext(
        ResourcePolicy(max_memory_bytes=1024 * 1024, max_temporary_bytes=limit),
        root,
    )


def test_combined_allocations_fail_before_output_creation(tmp_path):
    context = context_at(tmp_path, 200)
    context.reserve_temporary(tmp_path / "frames.rgb24", 100)
    with pytest.raises(ResourcePlanUnsatisfiableError):
        _rows_to_handle("output", (0,), lambda _: np.zeros(50, dtype=np.uint8),
                        context, file_backed=True)
    assert not (tmp_path / "output.npy").exists()
    assert len(context._temporary_sizes) == 1


def test_npy_header_counts_towards_budget(tmp_path):
    context = context_at(tmp_path, 50)
    with pytest.raises(ResourcePlanUnsatisfiableError):
        _rows_to_handle("output", (0,), lambda _: np.zeros(50, dtype=np.uint8),
                        context, file_backed=True)
    assert not list(tmp_path.iterdir())


def test_disk_full_rejects_allocation_without_reserving(tmp_path, monkeypatch):
    context = context_at(tmp_path)
    monkeypatch.setattr("pixelprobe.utils.resource_budget.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=10))
    with pytest.raises(ResourcePlanUnsatisfiableError):
        context.reserve_temporary(tmp_path / "nested" / "output.npy", 11)
    assert not context._temporary_sizes
    assert not list(tmp_path.iterdir())


def test_resumed_file_counts_once_and_needs_no_new_space(tmp_path, monkeypatch):
    existing = tmp_path / "partial.npy"
    existing.write_bytes(b"x" * 100)
    context = context_at(tmp_path, 120)
    monkeypatch.setattr("pixelprobe.utils.resource_budget.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=0))
    context.reserve_temporary(existing, 100)
    context.reserve_temporary(existing, 100)
    assert sum(context._temporary_sizes.values()) == 100
    with pytest.raises(ResourcePlanUnsatisfiableError):
        context.reserve_temporary(existing, 101)
    assert context._temporary_sizes[existing.resolve()] == 100


def test_release_requires_removed_file_and_restores_capacity(tmp_path):
    context = context_at(tmp_path, 100)
    first = tmp_path / "first"
    context.reserve_temporary(first, 100)
    first.write_bytes(b"x" * 100)
    with pytest.raises(ValueError):
        context.release_temporary(first)
    first.unlink()
    context.release_temporary(first)
    context.reserve_temporary(tmp_path / "second", 100)


def test_real_npy_outputs_share_budget(tmp_path):
    context = context_at(tmp_path, 220)
    first = _rows_to_handle("first", (0,), lambda _: np.arange(50, dtype=np.uint8),
                            context, file_backed=True)
    try:
        np.testing.assert_array_equal(first.materialize(), np.arange(50)[None, :])
        with pytest.raises(ResourcePlanUnsatisfiableError):
            _rows_to_handle("second", (0,), lambda _: np.zeros(50, dtype=np.uint8),
                            context, file_backed=True)
        assert not (tmp_path / "second.npy").exists()
    finally:
        first.close()


def test_frame_store_release_after_close(test_image: Path, tmp_path):
    context = context_at(tmp_path, 1024 * 1024)
    store = SharedFrameStore(test_image, context)
    store.decode()
    assert sum(context._temporary_sizes.values()) > 0
    store.close()
    assert not context._temporary_sizes
