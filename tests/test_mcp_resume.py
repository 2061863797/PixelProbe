"""MCP 显式恢复：真实 checkpoint、重启和失配拒绝。"""
from __future__ import annotations

import json
import threading
import shutil
from pathlib import Path

import pytest

from pixelprobe_mcp.config import ServerConfig, PathAccessError
from pixelprobe_mcp.jobs import JobManager
from pixelprobe_mcp.worker import WorkerError, execute_operation


def _payload(video):
    from pixelprobe.domain.geometry import PathGeometry
    from pixelprobe.domain.media import MediaSource
    from pixelprobe.domain.time import TemporalSelection
    from pixelprobe.engine import RepresentationRequest, OutputRequest
    request = RepresentationRequest(
        source=MediaSource(source_id="source_main", kind="file", uri=str(video)),
        selection=TemporalSelection(mode="all"), representation="xt",
        geometry=PathGeometry(type="line", coordinate_space_id="storage_pixels",
                              points=((0.0, 0.0), (10.0, 0.0))),
        output=OutputRequest(format="bundle", include_preview=False),
    )
    return {"media_path": str(video), "request": request.model_dump(mode="json")}


def _interrupted(tmp_path, video, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    from pixelprobe.engine.execution import LocalExecutionContext
    config = ServerConfig((tmp_path, video.parent), tmp_path / "artifacts")
    manager = JobManager(config)
    original = LocalExecutionContext.checkpoint_to
    def interrupt(context, target, record):
        original(context, target, record)
        raise RuntimeError("simulated interruption after atomic checkpoint")
    with monkeypatch.context() as patch:
        patch.setattr(LocalExecutionContext, "checkpoint_to", interrupt)
        patch.setattr(jobs, "run_operation_sync",
                      lambda config, op, payload, **kwargs: execute_operation(config, op, payload))
        job = manager.submit("generate_representation", _payload(video), timeout_seconds=10, persistent=True)
        job.future.result(10)
    assert job.status == "failed" and not job.result_ready
    assert (config.checkpoint_directory(job.checkpoint_id) / "checkpoint.json").is_file()
    manager.shutdown(wait=True)
    # 模拟服务在提交终态前退出，重启只能标记中断。
    record_path = tmp_path / "artifacts" / ".jobs" / f"{job.job_id}.json"
    record = json.loads(record_path.read_text("utf-8"))
    record["status"] = "running"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return config, job


def test_restart_requires_explicit_resume_and_real_worker_completes(tmp_path, test_video, monkeypatch):
    config, original = _interrupted(tmp_path, test_video, monkeypatch)
    manager = JobManager(config)
    try:
        record = manager.get(original.job_id)
        assert record["status"] == "interrupted"
        assert manager._outstanding == 0
        resumed = manager.resume(original.job_id)
        assert resumed.job_id != original.job_id
        assert resumed.resumed_from == original.job_id
        assert resumed.checkpoint_id == original.checkpoint_id
        assert resumed.timeout_seconds == original.timeout_seconds
        resumed.future.result(15)
        assert resumed.status == "succeeded", resumed.error
        result, _ = manager.result(resumed.job_id)
        assert Path(result["data"]["bundle_path"]).is_dir()
        assert manager.get(original.job_id)["status"] == "interrupted"
    finally:
        manager.shutdown(wait=True)


@pytest.mark.parametrize("damage", ["missing", "request"])
def test_invalid_recovery_is_rejected(tmp_path, test_video, monkeypatch, damage):
    config, original = _interrupted(tmp_path, test_video, monkeypatch)
    if damage == "missing":
        (config.checkpoint_directory(original.checkpoint_id) / "checkpoint.json").unlink()
    else:
        path = config.artifact_root / ".jobs" / f"{original.job_id}.request.json"
        path.write_text("{}", encoding="utf-8")
    manager = JobManager(config)
    try:
        with pytest.raises(WorkerError, match="MCP_RESUME_UNAVAILABLE"):
            manager.resume(original.job_id)
        assert manager._outstanding == 0
    finally:
        manager.shutdown(wait=True)


def test_changed_request_rejected_by_checkpoint_validation(tmp_path, test_video, monkeypatch):
    config, original = _interrupted(tmp_path, test_video, monkeypatch)
    path = config.artifact_root / ".jobs" / f"{original.job_id}.request.json"
    value = json.loads(path.read_text("utf-8"))
    value["payload"]["request"]["selection"] = {"mode": "indices", "requested_indices": [0]}
    path.write_text(json.dumps(value), encoding="utf-8")
    manager = JobManager(config)
    try:
        resumed = manager.resume(original.job_id)
        resumed.future.result(15)
        assert resumed.status == "failed"
        assert not resumed.result_ready
        assert "CHECKPOINT" in resumed.error_code
    finally:
        manager.shutdown(wait=True)


def test_duplicate_resume_cannot_share_active_checkpoint(tmp_path, test_video, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    config, original = _interrupted(tmp_path, test_video, monkeypatch)
    release = threading.Event()
    def execute(*args, **kwargs):
        assert release.wait(5)
        return {}
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(config)
    try:
        resumed = manager.resume(original.job_id)
        with pytest.raises(WorkerError, match="MCP_RESUME_BUSY"):
            manager.resume(original.job_id)
        release.set()
        resumed.future.result(5)
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_checkpoint_path_rejects_traversal(tmp_path):
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    with pytest.raises(PathAccessError):
        config.checkpoint_directory("../outside", create=True)
    assert not config.artifact_root.exists()


def test_changed_source_rejected_by_checkpoint_validation(tmp_path, test_video, monkeypatch):
    source = tmp_path / "source.mp4"
    shutil.copyfile(test_video, source)
    config, original = _interrupted(tmp_path, source, monkeypatch)
    # 合法尾部数据改变完整哈希，视频仍可解码。
    with source.open("ab") as output:
        output.write(b"changed-input")
    manager = JobManager(config)
    try:
        resumed = manager.resume(original.job_id)
        resumed.future.result(15)
        assert resumed.status == "failed" and not resumed.result_ready
        assert "CHECKPOINT" in resumed.error_code
    finally:
        manager.shutdown(wait=True)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_resume_tool_over_mcp_session(tmp_path, test_video, monkeypatch):
    import pixelprobe_mcp.server as server
    from pixelprobe_mcp.service import PixelProbeService
    from mcp.shared.memory import create_connected_server_and_client_session
    config, original = _interrupted(tmp_path, test_video, monkeypatch)
    manager = JobManager(config)
    monkeypatch.setattr(server, "SERVICE", PixelProbeService(config))
    monkeypatch.setattr(server, "_JOB_MANAGER", manager)
    try:
        async with create_connected_server_and_client_session(server.mcp, raise_exceptions=True) as session:
            result = await session.call_tool("pixelprobe_resume_job", {"job_id": original.job_id})
            assert not result.isError
            data = result.structuredContent["data"]
            assert data["resumed_from"] == original.job_id
            job = manager.get(data["job_id"])
            import anyio
            await anyio.to_thread.run_sync(lambda: job.future.result(15))
            result = await session.call_tool("pixelprobe_get_job_result", {"job_id": job.job_id})
            assert not result.isError
            assert result.structuredContent["data"]["bundle_path"]
    finally:
        manager.shutdown(wait=True)
