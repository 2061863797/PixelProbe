"""资源控制验收：真实写入、真实 spawn 和失败后的可恢复性。"""
import errno
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from pixelprobe.artifacts import BundleWriter, BundleReader
from pixelprobe.artifacts.array_io import save_npy
from pixelprobe.artifacts.zarr_io import save_array_handle_zarr, save_zarr
from pixelprobe.domain.tensor import MemoryArrayHandle
from pixelprobe.domain.errors import MaterializationLimitExceededError
from pixelprobe.engine.errors import ResourcePlanUnsatisfiableError, ExecutionCancelledError
from pixelprobe.engine.execution import LocalExecutionContext, CheckpointRecord, encoded_state
from pixelprobe.operators.base import ResourcePolicy
from pixelprobe.utils.resource_budget import ResourceBudget, budget_scope
from pixelprobe_mcp.config import ServerConfig
from pixelprobe_mcp.resource_plan import media_plan, plan_operation
from pixelprobe_mcp.worker import run_operation_sync, WorkerError

MIB = 1024 * 1024


def request(selection=None):
    return {
        "source": {"source_id": "s", "kind": "file", "uri": "unused"},
        "selection": selection or {"mode": "all"},
        "representation": "frames",
        "output": {"format": "bundle", "include_preview": False},
    }


def tensor():
    from pixelprobe.evidence import _ela_data_tensor
    return _ela_data_tensor("test", np.zeros((16, 16, 3), dtype=np.float32), attributes={})


def test_plan_reads_header_without_loading_pixels(tmp_path, monkeypatch):
    from PIL.PngImagePlugin import PngImageFile
    source = tmp_path / "header.png"
    Image.new("RGB", (64, 32)).save(source)
    monkeypatch.setattr(PngImageFile, "load", lambda *_: pytest.fail("预算阶段不得解码"))
    result = plan_operation(ServerConfig((tmp_path,), tmp_path / "out"),
                            "evidence", {"media_path": str(source)})
    assert result["full_resolution_pixels"] == 2048
    assert result["analysis_memory_bytes"] == 2048 * 128


def test_local_plan_uses_required_prefix_not_entire_long_video():
    info = SimpleNamespace(width=32, height=32, media_type="video", frame_count=100000)
    selection = {"mode": "frame_interval", "requested_start_frame": 100,
                 "requested_end_frame_exclusive": 110, "sample_every": 2}
    plan = media_plan("generate_representation", info, {"request": request(selection)})
    assert plan["decoded_frames_upper_bound"] == 110
    assert plan["stored_frames_estimate"] == 5
    assert not plan["full_scan"]


def test_sparse_plan_counts_unsampled_end_boundary():
    info = SimpleNamespace(width=32, height=32, media_type="video", frame_count=200)
    selection = {"mode": "indices", "requested_indices": [1, 50, 100, 199], "sample_every": 2}
    plan = media_plan("generate_representation", info, {"request": request(selection)})
    assert plan["decoded_frames_upper_bound"] == 200
    assert plan["stored_frames_estimate"] == 2


def test_time_selection_reports_full_scan_without_fps_mapping():
    info = SimpleNamespace(width=32, height=32, media_type="video", frame_count=200)
    selection = {"mode": "time_interval", "requested_start_seconds": 0.1,
                 "requested_end_seconds": 0.2}
    plan = media_plan("generate_representation", info, {"request": request(selection)})
    assert plan["full_scan"] and plan["decoded_frames_per_pass_estimate"] == 200
    assert plan["decoded_frames_upper_bound"] is None


def test_crop_cannot_bypass_decode_budget(tmp_path):
    from pixelprobe_mcp.service import PixelProbeService, McpResourceLimitError
    from pixelprobe_mcp.models import RectInput
    service = PixelProbeService(ServerConfig((tmp_path,), tmp_path / "out"))
    with pytest.raises(McpResourceLimitError):
        service._ensure_frame_budget(SimpleNamespace(width=20000, height=20000),
                                     RectInput(x=0, y=0, width=1, height=1),
                                     include_image_payload=True)


def test_decode_budget_checked_before_image_load(tmp_path, monkeypatch):
    from pixelprobe.core.image_reader import ImageReader
    from PIL.PngImagePlugin import PngImageFile
    source = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(source)
    monkeypatch.setattr(PngImageFile, "load", lambda *_: pytest.fail("不得开始解码"))
    with budget_scope(ResourceBudget(100, MIB)):
        with pytest.raises(MaterializationLimitExceededError):
            with ImageReader() as reader:
                reader.open(source)


def test_checkpoint_replacement_counts_old_and_new_and_keeps_old(tmp_path):
    context = LocalExecutionContext(ResourcePolicy(max_memory_bytes=MIB,
                                    max_temporary_bytes=500), tmp_path)
    record = CheckpointRecord(plan_id="p", request_sha256="a", input_sha256="b",
                              operator_versions={}, completed_chunks=(),
                              state_base64=encoded_state(b"x"))
    target = context.checkpoint("test", record)
    before = target.read_bytes()
    larger = record.model_copy(update={"state_base64": encoded_state(b"x" * 200)})
    with pytest.raises(ResourcePlanUnsatisfiableError):
        context.checkpoint_to(target, larger)
    assert target.read_bytes() == before
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("format", ["npy", "zarr"])
def test_bundle_quota_failure_keeps_target_absent(tmp_path, format):
    target = tmp_path / "result.bundle"
    with budget_scope(ResourceBudget(MIB, 100)):
        with pytest.raises(ResourcePlanUnsatisfiableError):
            BundleWriter().write(target, (tensor(),), array_format=format)
    assert not target.exists()
    assert not list(tmp_path.iterdir())


def test_bundle_commit_budget_tracks_renamed_tree(tmp_path):
    budget = ResourceBudget(MIB, MIB)
    target = tmp_path / "result.bundle"
    with budget_scope(budget):
        BundleWriter().write(target, (tensor(),))
    BundleReader().open(target, verify="full")
    assert budget.sizes
    assert budget.bundle_verification_passes == 2
    stored_bytes = sum(p.stat().st_size for p in target.rglob("*")
                       if p.is_file() and p.name != "manifest.json")
    assert budget.bundle_verified_bytes == stored_bytes * 2
    assert all(target in path.parents and path.is_file() for path in budget.sizes)
    assert sum(budget.sizes.values()) >= sum(p.stat().st_size for p in target.rglob("*") if p.is_file())


@pytest.mark.parametrize("direct", [False, True])
def test_zarr_exact_encoded_writes_obey_quota(tmp_path, direct):
    values = np.arange(4096, dtype=np.uint16).reshape(64, 64)
    target = tmp_path / "array.zarr"
    with budget_scope(ResourceBudget(MIB, 100)):
        with pytest.raises(ResourcePlanUnsatisfiableError):
            if direct:
                save_zarr(values, target, chunk_shape=(16, 16))
            else:
                save_array_handle_zarr(MemoryArrayHandle(values), target, chunk_shape=(16, 16))
    assert not target.exists() and not list(tmp_path.iterdir())
    budget = ResourceBudget(MIB, MIB)
    with budget_scope(budget):
        actual = save_array_handle_zarr(MemoryArrayHandle(values), target, chunk_shape=(16, 16))
    np.testing.assert_array_equal(actual.materialize(), values)
    assert sum(budget.sizes.values()) == sum(p.stat().st_size for p in target.rglob("*") if p.is_file())


def test_npy_failure_does_not_charge_nonexistent_file(tmp_path):
    budget = ResourceBudget(MIB, 16)
    with budget_scope(budget):
        with pytest.raises(ResourcePlanUnsatisfiableError):
            save_npy(np.arange(100), tmp_path / "out.npy")
    assert not budget.sizes
    assert not list(tmp_path.iterdir())


def test_enospc_has_stable_error_and_no_published_bundle(tmp_path, monkeypatch):
    import pixelprobe.artifacts.bundle as module
    def disk_full(*args):
        raise OSError(errno.ENOSPC, "disk full")
    monkeypatch.setattr(module, "_write_bytes", disk_full)
    with budget_scope(ResourceBudget(MIB, MIB)):
        with pytest.raises(ResourcePlanUnsatisfiableError):
            BundleWriter().write(tmp_path / "out.bundle", (tensor(),))
    assert not list(tmp_path.iterdir())


def test_cache_quota_failure_preserves_existing_entry(tmp_path):
    from pixelprobe.engine import LocalArrayCache, CacheKeyInput
    cache = LocalArrayCache(tmp_path / "cache")
    key = CacheKeyInput(input_content_sha256="a" * 64, operator_name="sample",
                        operator_version="1", canonical_config={}, input_tensor_descriptors=(),
                        dtype="uint8", precision="decoded",
                        execution_semantics_version="1", artifact_role="data")
    cache.put(key, np.zeros(100, dtype=np.uint8)).close()
    with budget_scope(ResourceBudget(MIB, 10)):
        with pytest.raises(ResourcePlanUnsatisfiableError):
            cache.get(key)
    assert not list(cache.quarantine.iterdir())
    found = cache.get(key)
    assert found.handle is not None
    found.handle.close()


def test_real_worker_memory_limit_reaps_then_next_request_succeeds(tmp_path):
    source = tmp_path / "memory.png"
    Image.new("RGB", (16, 16), (7, 8, 9)).save(source)
    config = ServerConfig((tmp_path,), tmp_path / "out")
    payload = {"media_path": str(source), "points": [{"x": 0, "y": 0}]}
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(WorkerError, match="MCP_RESOURCE_LIMIT"):
        run_operation_sync(config, "read_pixels", payload, timeout_seconds=10,
                           memory_limit_bytes=1)
    assert {p.pid for p in multiprocessing.active_children()} == before
    result = run_operation_sync(config, "read_pixels", payload, timeout_seconds=10)
    assert result.data["pixels"][0]["rgb"] == {"r": 7, "g": 8, "b": 9}
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_real_job_resource_failure_is_not_claimable_and_releases(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    source = tmp_path / "job.png"
    Image.new("RGB", (16, 16)).save(source)
    manager = jobs.JobManager(ServerConfig((tmp_path,), tmp_path / "out"))
    original = jobs.run_operation_sync
    def limited(*args, **kwargs):
        kwargs["memory_limit_bytes"] = 1
        return original(*args, **kwargs)
    monkeypatch.setattr(jobs, "run_operation_sync", limited)
    try:
        job = manager.submit("read_pixels", {"media_path": str(source),
                             "points": [{"x": 0, "y": 0}]}, timeout_seconds=10, persistent=True)
        job.future.result(15)
        assert job.status == "failed" and job.error_code == "MCP_RESOURCE_LIMIT"
        with pytest.raises(WorkerError, match="MCP_JOB_NOT_READY"):
            manager.result(job.job_id)
        assert manager.allocated_memory_bytes == manager.allocated_temporary_bytes == 0
        monkeypatch.setattr(jobs, "run_operation_sync", original)
        following = manager.submit("read_pixels", {"media_path": str(source),
                                   "points": [{"x": 0, "y": 0}]}, timeout_seconds=10, persistent=True)
        following.future.result(15)
        assert following.status == "succeeded"
        assert following.resource_plan["full_resolution_pixels"] == 256
        assert following.resource_usage["peak_worker_rss_bytes"] > 0
    finally:
        manager.shutdown(wait=True)


def test_vfr_partial_generation_has_exact_decode_cost(vfr_video, tmp_path):
    import pixelprobe
    from conftest import vfr_frame
    payload = request({"mode": "frame_interval", "requested_start_frame": 3,
                       "requested_end_frame_exclusive": 8, "sample_every": 2})
    payload["source"]["uri"] = str(vfr_video)
    payload["resources"] = {"max_memory_bytes": MIB, "max_temporary_bytes": MIB}
    result = pixelprobe.generate(payload, output_path=tmp_path / "vfr.bundle")
    assert result.decode_passes == 1
    assert result.source_frames_decoded == 8
    assert result.frames_converted == result.frames_stored == 3
    tensor = result.request_tensors[0][0]
    try:
        np.testing.assert_array_equal(tensor.data.materialize(),
                                      np.stack([vfr_frame(i) for i in (3, 5, 7)]))
    finally:
        tensor.data.close()


def test_high_bit_depth_native_storage_is_charged_and_exact(tmp_path):
    from pixelprobe.engine.frame_store import SharedFrameStore
    source = tmp_path / "16bit.png"
    values = np.arange(256, dtype=np.uint16).reshape(16, 16) * 257
    Image.fromarray(values).save(source)
    context = LocalExecutionContext(ResourcePolicy(max_memory_bytes=MIB,
                                                   max_temporary_bytes=MIB), tmp_path / "temp")
    with budget_scope(context.budget):
        with SharedFrameStore(source, context) as store:
            assert sum(context.budget.sizes.values()) == values.nbytes + values.size * 3
            np.testing.assert_array_equal(store.native_image.materialize(), values)
    assert not context.budget.sizes


def test_parent_result_quota_does_not_publish_oversized_payload(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    manager = jobs.JobManager(ServerConfig((tmp_path,), tmp_path / "out"))
    monkeypatch.setattr(jobs, "run_operation_sync", lambda *args, **kwargs: {"data": "x" * (5 * MIB)})
    try:
        job = manager.submit("read_pixels", {}, timeout_seconds=10, persistent=True)
        job.future.result(10)
        assert job.status == "failed" and not job.result_ready
        assert not (tmp_path / "out" / ".jobs" / (job.job_id + ".result.json")).exists()
        assert manager.allocated_memory_bytes == manager.allocated_temporary_bytes == 0
    finally:
        manager.shutdown(wait=True)


def test_two_real_workers_never_reserve_more_than_total(tmp_path):
    from pixelprobe_mcp.jobs import JobManager
    source = tmp_path / "parallel.png"
    Image.new("RGB", (32, 32)).save(source)
    config = ServerConfig((tmp_path,), tmp_path / "out", workers=2)
    manager = JobManager(config)
    try:
        jobs = [manager.submit("read_pixels", {"media_path": str(source),
                               "points": [{"x": 0, "y": 0}]},
                               timeout_seconds=10, persistent=True) for _ in range(2)]
        with manager._lock:
            assert manager.allocated_memory_bytes <= config.total_memory_bytes
            assert manager.allocated_temporary_bytes <= config.total_temporary_bytes
        for job in jobs:
            job.future.result(15)
            assert job.status == "succeeded", job.error
            assert job.resource_usage["peak_worker_rss_bytes"] <= job.required_memory_bytes
        assert manager.allocated_memory_bytes == manager.allocated_temporary_bytes == 0
    finally:
        manager.shutdown(wait=True)


def _write_lossless_video(path, count, *, duplicate_pts=False):
    import av
    from fractions import Fraction
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width = stream.height = 16
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, 1000)
        for index in range(count):
            frame = av.VideoFrame.from_ndarray(
                np.full((16, 16, 3), index % 256, dtype=np.uint8), format="rgb24")
            frame.time_base = Fraction(1, 1000)
            frame.pts = 40 if duplicate_pts and index == 2 else index * 40
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def test_real_duplicate_pts_keeps_sequential_identity_and_quota(tmp_path):
    from pixelprobe.core.video_reader import VideoReader
    from pixelprobe.engine.frame_store import SharedFrameStore
    from pixelprobe.domain.time import TemporalSelection
    source = tmp_path / "duplicate-pts.mkv"
    _write_lossless_video(source, 4, duplicate_pts=True)
    with VideoReader() as reader:
        reader.open(source)
        assert reader.build_pts_index() is None
    context = LocalExecutionContext(ResourcePolicy(max_memory_bytes=MIB,
                                                   max_temporary_bytes=1536), tmp_path / "frames")
    selection = TemporalSelection(mode="time_interval", requested_start_seconds=0.04,
                                  requested_end_seconds=0.05)
    with budget_scope(context.budget):
        with SharedFrameStore(source, context, (selection,)) as store:
            assert store.source_frames_decoded == 4
            assert store.presentation_indices == (1, 2)
            assert store.times == (0.04, 0.04)
            np.testing.assert_array_equal(store.frames.materialize(),
                np.stack([np.full((16, 16, 3), i, np.uint8) for i in (1, 2)]))
            assert sum(context.budget.sizes.values()) == 1536
    assert not context.budget.sizes


def test_real_long_video_partial_mcp_job_uses_five_decodes(tmp_path):
    from pixelprobe_mcp.jobs import JobManager
    source = tmp_path / "long.mkv"
    _write_lossless_video(source, 2001)
    manager = JobManager(ServerConfig((tmp_path,), tmp_path / "out",
                                     total_temporary_bytes=32 * MIB))
    payload = {"media_path": str(source), "request": request({
        "mode": "frame_interval", "requested_start_frame": 1,
        "requested_end_frame_exclusive": 5, "sample_every": 2})}
    try:
        job = manager.submit("generate_representation", payload, timeout_seconds=20, persistent=True)
        job.future.result(25)
        assert job.status == "succeeded", job.error
        result, _ = manager.result(job.job_id)
        assert result["data"]["source_frames_decoded"] == 5
        assert result["data"]["frames_converted"] == result["data"]["frames_stored"] == 2
        assert job.resource_plan["decoded_frames_upper_bound"] == 5
        assert job.resource_usage["peak_managed_disk_bytes"] < 32 * MIB
    finally:
        manager.shutdown(wait=True)
