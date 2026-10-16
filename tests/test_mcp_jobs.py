"""MCP 有界任务队列、持久化、取消与结果领取。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from dataclasses import replace

import pytest
from PIL import Image

from pixelprobe_mcp.config import ServerConfig
from pixelprobe_mcp.jobs import JobManager, QueueFullError


def _config(root: Path, *, workers: int = 1, queue_size: int = 1) -> ServerConfig:
    return ServerConfig((root,), root / "artifacts", workers=workers, queue_size=queue_size)


def test_fifo_queue_is_bounded_and_runs_in_submission_order(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    entered: list[int] = []
    release = threading.Event()
    def execute(_config, _operation, payload, **_kwargs):
        entered.append(payload["order"])
        if payload["order"] == 1:
            release.wait(2)
        return {"order": payload["order"]}
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(_config(tmp_path))
    first = manager.submit("capability", {"order": 1}, timeout_seconds=5, persistent=False)
    second = manager.submit("capability", {"order": 2}, timeout_seconds=5, persistent=False)
    with pytest.raises(QueueFullError, match="MCP_QUEUE_FULL"):
        manager.submit("capability", {"order": 3}, timeout_seconds=5, persistent=False)
    release.set()
    assert first.future is not None and first.future.result(3) == {"order": 1}
    assert second.future is not None and second.future.result(3) == {"order": 2}
    assert entered == [1, 2]


def test_persistent_result_and_restart_recovery(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    monkeypatch.setattr(
        jobs, "run_operation_sync", lambda *_args, **_kwargs: {"success": True, "data": {"x": 1}},
    )
    manager = JobManager(_config(tmp_path))
    job = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
    assert job.future is not None
    job.future.result(3)
    result, images = manager.result(job.job_id)
    assert result["data"] == {"x": 1} and images == []
    record = tmp_path / "artifacts" / ".jobs" / f"{job.job_id}.json"
    value = json.loads(record.read_text(encoding="utf-8"))
    value["status"] = "running"
    record.write_text(json.dumps(value), encoding="utf-8")
    manager.shutdown(wait=True)
    restarted = JobManager(_config(tmp_path))
    recovered = restarted.get(job.job_id)
    assert isinstance(recovered, dict) and recovered["status"] == "interrupted"
    restarted.shutdown(wait=True)


def test_cancel_running_job_is_idempotent(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    def execute(_config, _operation, _payload, *, cancel_event, **_kwargs):
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise jobs.WorkerError("MCP_CANCELLED：任务已取消")
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(_config(tmp_path))
    job = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
    deadline = time.time() + 2
    while job.status != "running" and time.time() < deadline:
        time.sleep(0.01)
    assert manager.cancel(job.job_id)["status"] in {"running", "cancelled"}
    assert job.future is not None
    job.future.result(3)
    assert manager.cancel(job.job_id)["status"] == "cancelled"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("resource", ["memory", "temporary"])
def test_resource_budget_fifo_and_release(tmp_path, monkeypatch, resource):
    import pixelprobe_mcp.jobs as jobs
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(jobs, "estimate_operation_resources", lambda op, p: (p["cost"], p["cost"]))
    config = replace(_config(tmp_path, workers=3, queue_size=3),
                     total_memory_bytes=10 if resource == "memory" else 100,
                     total_temporary_bytes=10 if resource == "temporary" else 100)
    manager = JobManager(config)
    def execute(config, op, payload, **kwargs):
        calls.append(payload["order"])
        if payload["order"] == 1:
            entered.set()
            assert release.wait(3)
        return payload
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    try:
        first = manager.submit("capability", {"order": 1, "cost": 6}, timeout_seconds=5, persistent=False)
        assert entered.wait(2)
        second = manager.submit("capability", {"order": 2, "cost": 6}, timeout_seconds=5, persistent=False)
        third = manager.submit("capability", {"order": 3, "cost": 1}, timeout_seconds=5, persistent=False)
        with manager._lock:
            assert second.status == third.status == "queued"
            assert manager.allocated_memory_bytes == manager.allocated_temporary_bytes == 6
        release.set()
        for job in (first, second, third):
            job.future.result(3)
        assert calls == [1, 2, 3]
        assert manager.allocated_memory_bytes == manager.allocated_temporary_bytes == 0
        assert manager._outstanding == 0
    finally:
        release.set()
        manager.shutdown(wait=True)


@pytest.mark.parametrize("finish", ["cancel", "timeout"])
def test_resource_wait_finishes_without_starting_worker(tmp_path, monkeypatch, finish):
    import pixelprobe_mcp.jobs as jobs
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {}
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(replace(_config(tmp_path, workers=2),
                                total_memory_bytes=64 * 1024 * 1024,
                                queue_timeout_seconds=0.2))
    try:
        first = manager.submit("capability", {}, timeout_seconds=5, persistent=False)
        assert entered.wait(2)
        second = manager.submit("capability", {}, timeout_seconds=5, persistent=False)
        # Future 已启动但媒体进程仍未启动的等待路径。
        deadline = time.monotonic() + 1
        while not second.future.running() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert second.future.running()
        assert second.status == "queued" and second.started_at is None
        if finish == "cancel":
            manager.cancel(second.job_id)
        second.future.result(2)
        assert second.status == ("cancelled" if finish == "cancel" else "timeout")
        assert len(calls) == 1
        assert manager._outstanding == 1
        assert manager.allocated_memory_bytes == 64 * 1024 * 1024
        release.set()
        first.future.result(2)
        assert manager.allocated_memory_bytes == manager._outstanding == 0
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_resource_rejection_has_no_side_effects(tmp_path):
    from pixelprobe_mcp.jobs import ResourceBudgetExceededError
    manager = JobManager(replace(_config(tmp_path), total_memory_bytes=1))
    try:
        with pytest.raises(ResourceBudgetExceededError, match="MCP_RESOURCE_LIMIT"):
            manager.submit("capability", {}, timeout_seconds=5, persistent=True)
        assert manager._jobs == {}
        assert manager._outstanding == manager.allocated_memory_bytes == 0
        assert not (tmp_path / "artifacts").exists()
    finally:
        manager.shutdown(wait=True)


@pytest.mark.parametrize("failure", ["timeout", "crash", "state"])
def test_resources_released_on_worker_or_state_failure(tmp_path, monkeypatch, failure):
    import pixelprobe_mcp.jobs as jobs
    manager = JobManager(_config(tmp_path))
    def execute(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError
        if failure == "crash":
            raise jobs.WorkerError("MCP_WORKER_CRASHED：异常退出")
        return {}
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    original = manager._persist
    def persist(job):
        if failure == "state" and job.status == "running":
            raise OSError("disk full")
        original(job)
    monkeypatch.setattr(manager, "_persist", persist)
    try:
        job = manager.submit("capability", {}, timeout_seconds=5, persistent=False)
        job.future.result(3)
        assert job.status == ("timeout" if failure == "timeout" else "failed")
        assert not job.resource_reserved and not job.result_ready
        assert manager.allocated_memory_bytes == manager.allocated_temporary_bytes == manager._outstanding == 0
    finally:
        manager.shutdown(wait=True)


@pytest.mark.anyio
async def test_resource_limit_is_tool_error_for_both_interfaces(tmp_path, monkeypatch):
    import pixelprobe_mcp.server as server
    from pixelprobe_mcp.service import PixelProbeService
    from mcp.server.fastmcp.exceptions import ToolError
    manager = JobManager(replace(_config(tmp_path), total_memory_bytes=1))
    monkeypatch.setattr(server, "SERVICE", PixelProbeService(manager.config))
    monkeypatch.setattr(server, "_JOB_MANAGER", manager)
    try:
        for wait in (True, False):
            with pytest.raises(ToolError, match="MCP_RESOURCE_LIMIT"):
                await server.pixelprobe_overview(media_path=str(tmp_path / "input.png"), wait=wait)
    finally:
        manager.shutdown(wait=True)


def test_submission_disk_failure_does_not_consume_capacity(tmp_path, monkeypatch):
    manager = JobManager(_config(tmp_path))
    def fail(_job):
        raise OSError("磁盘已满")
    monkeypatch.setattr(manager, "_persist", fail)
    with pytest.raises(OSError):
        manager.submit("capability", {}, timeout_seconds=5, persistent=True)
    assert manager._outstanding == 0
    assert manager._jobs == {}
    manager.shutdown(wait=True)


def test_final_state_failure_releases_slot_and_hides_result(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    manager = JobManager(_config(tmp_path))
    monkeypatch.setattr(jobs, "run_operation_sync", lambda *args, **kwargs: {"ok": True})
    original = manager._persist
    def fail_final(job):
        if job.finished_at:
            raise OSError("磁盘已满")
        original(job)
    monkeypatch.setattr(manager, "_persist", fail_final)
    job = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
    job.future.result(3)
    assert manager._outstanding == 0
    assert job.status == "failed" and not job.result_ready
    with pytest.raises(jobs.WorkerError, match="MCP_JOB_NOT_READY"):
        manager.result(job.job_id)
    manager.shutdown(wait=True)


def test_shutdown_finalizes_queued_jobs_once(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    entered = threading.Event()
    def execute(*args, cancel_event, **kwargs):
        entered.set()
        cancel_event.wait(3)
        return {"ok": True}
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(_config(tmp_path))
    first = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
    assert entered.wait(2)
    second = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
    manager.shutdown(wait=True)
    assert first.status == second.status == "cancelled"
    assert not first.result_ready and not second.result_ready
    assert manager._outstanding == 0
    manager.cancel(second.job_id)
    assert manager._outstanding == 0
    with pytest.raises(RuntimeError, match="已关闭"):
        manager.submit("capability", {}, timeout_seconds=5, persistent=False)


def test_queue_timeout_finishes_while_worker_remains_busy(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    entered, release = threading.Event(), threading.Event()
    calls = []
    def execute(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"ok": True}
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(replace(_config(tmp_path), queue_timeout_seconds=0.2))
    try:
        first = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
        assert entered.wait(2)
        queued = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
        deadline = time.monotonic() + 2
        while queued.status == "queued" and time.monotonic() < deadline:
            time.sleep(0.01)
        with manager._lock:
            assert queued.status == "timeout"
            assert queued.error_code == "MCP_QUEUE_TIMEOUT"
            assert manager._outstanding == 1
        assert not first.future.done()
        assert len(calls) == 1
        assert manager.cancel(queued.job_id)["status"] == "timeout"
        replacement = manager.submit("capability", {}, timeout_seconds=5, persistent=False)
        release.set()
        first.future.result(3)
        replacement.future.result(3)
        assert manager._outstanding == 0
    finally:
        release.set()
        manager.shutdown(wait=True)


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_image_result_requires_committed_image(tmp_path, monkeypatch, damage):
    import pixelprobe_mcp.jobs as jobs
    images = [("frame", b"png-0"), ("curve", b"png-1")]
    monkeypatch.setattr(jobs, "run_operation_sync", lambda *args, **kwargs: ({"ok": True}, images))
    manager = JobManager(_config(tmp_path))
    try:
        job = manager.submit("capability", {}, timeout_seconds=5, persistent=True)
        job.future.result(3)
        assert manager.result(job.job_id) == ({"ok": True}, images)
        # 多张图片按序号分别提交并各自校验；任何一张缺失或被改动都不可领取
        image = tmp_path / "artifacts" / ".jobs" / f"{job.job_id}.1.png"
        if damage == "missing":
            image.unlink()
        else:
            image.write_bytes(b"damaged")
        with pytest.raises(jobs.WorkerError, match="MCP_RESULT_INVALID"):
            manager.result(job.job_id)
    finally:
        manager.shutdown(wait=True)


def test_recovery_does_not_treat_result_payload_as_job_record(tmp_path):
    root = tmp_path / "artifacts" / ".jobs"
    root.mkdir(parents=True)
    result = root / ("a" * 32 + ".result.json")
    result.write_text('{"status":"running","result":42}', encoding="utf-8")
    manager = JobManager(_config(tmp_path))
    manager._ensure_store()
    assert json.loads(result.read_text(encoding="utf-8")) == {"status": "running", "result": 42}
    manager.shutdown(wait=True)


def test_job_lookup_rejects_traversal_before_access(tmp_path):
    from pixelprobe_mcp.jobs import JobNotFoundError
    manager = JobManager(_config(tmp_path))
    with pytest.raises(JobNotFoundError):
        manager.get("../outside")
    assert not (tmp_path / "artifacts").exists()
    manager.shutdown(wait=True)


@pytest.mark.anyio
async def test_mcp_submit_poll_and_get_result(tmp_path, monkeypatch):
    import pixelprobe_mcp.server as server
    from pixelprobe_mcp.service import PixelProbeService
    source = tmp_path / "job.png"
    Image.new("RGB", (1, 1), (7, 8, 9)).save(source)
    monkeypatch.setattr(server, "SERVICE", PixelProbeService(_config(tmp_path)))
    monkeypatch.setattr(server, "_JOB_MANAGER", None)
    submitted = await server.pixelprobe_measure(
        media_path=str(source), what="pixel", at="point(0,0)", wait=False,
    )
    job_id = submitted.structuredContent["data"]["job_id"]
    assert submitted.structuredContent["data"]["capability"] == "pixel"
    deadline = time.time() + 30
    while time.time() < deadline:
        status = (await server.pixelprobe_job("get", job_id)).structuredContent["data"]
        if status["status"] not in {"queued", "running"}:
            break
        await __import__("anyio").sleep(0.02)
    assert status["status"] == "succeeded"
    result = await server.pixelprobe_job("result", job_id)
    assert result.structuredContent["data"]["pixels"][0]["rgb"] == {"r": 7, "g": 8, "b": 9}
    with pytest.raises(Exception, match="MCP_JOB_NOT_FOUND"):
        await server.pixelprobe_job("get", "0" * 32)
    server._job_manager().shutdown(wait=True)


@pytest.mark.parametrize("capability", ["evidence", "first_change", "compare_media", "quality_overview"])
def test_phone_sized_image_passes_mcp_resource_precheck(tmp_path, capability):
    """回归：计划内存 = 运行时余量 + 分析内存，默认上限必须容得下 1086×1448 这类常见尺寸。"""
    from pixelprobe_mcp.jobs import estimate_operation_resources
    from pixelprobe_mcp.resource_plan import plan_operation
    image = tmp_path / "phone.png"
    Image.new("RGB", (1086, 1448), (10, 20, 30)).save(image)
    payload = {"capability": capability, "media_path": str(image), "at": None,
               "options": {}, "refs": {}}
    limit, _ = estimate_operation_resources("capability", payload)
    plan = plan_operation(_config(tmp_path), "capability", payload)
    assert plan["memory_bytes"] <= limit


MIB = 1024 * 1024


def _memory_config(root: Path) -> ServerConfig:
    return ServerConfig((root,), root / "artifacts", workers=2, queue_size=4,
                        total_memory_bytes=2048 * MIB)


@pytest.mark.parametrize("failure", [
    "MCP_RESOURCE_LIMIT：工作进程实际内存超过预算，已停止执行",
    "MCP_RESOURCE_LIMIT：工作进程内存分配失败",
    "MATERIALIZATION_LIMIT_EXCEEDED：Farneback 完整分辨率工作集至少需要 1 字节",
])
def test_memory_failure_retries_once_with_whole_budget(tmp_path, monkeypatch, failure):
    """内存类失败后用整份总预算单独重跑一次，成功后结果可领取，并记录首次失败。"""
    import pixelprobe_mcp.jobs as jobs
    limits: list[int] = []

    def execute(_config, _operation, _payload, **kwargs):
        limits.append(kwargs["memory_limit_bytes"])
        if len(limits) == 1:
            raise jobs.WorkerError(failure)
        return {"ok": True}

    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(_memory_config(tmp_path))
    try:
        job = manager.submit("capability", {"capability": "frame", "media_path": "v.mkv"},
                             timeout_seconds=5, persistent=False)
        job.future.result(10)
        assert job.status == "succeeded" and job.result == {"ok": True}
        assert limits == [1024 * MIB, 2048 * MIB]
        assert job.memory_retry["memory_limit_bytes"] == 1024 * MIB
        assert job.memory_retry["retry_memory_limit_bytes"] == 2048 * MIB
        assert manager.allocated_memory_bytes == 0
    finally:
        manager.shutdown(wait=True)


def test_non_memory_failure_and_second_memory_failure_are_not_retried(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    calls: list[str] = []

    def execute(_config, _operation, payload, **_kwargs):
        calls.append(payload["capability"])
        if payload["capability"] == "frame":
            raise jobs.WorkerError("UNSUPPORTED_MEDIA：无法识别的图片格式，或文件已损坏")
        raise jobs.WorkerError("MCP_RESOURCE_LIMIT：工作进程实际内存超过预算，已停止执行")

    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(_memory_config(tmp_path))
    try:
        broken = manager.submit("capability", {"capability": "frame", "media_path": "v.mkv"},
                                timeout_seconds=5, persistent=False)
        broken.future.result(10)
        heavy = manager.submit("capability", {"capability": "scan", "media_path": "v.mkv"},
                               timeout_seconds=5, persistent=False)
        heavy.future.result(10)
        assert calls == ["frame", "scan", "scan"]
        assert broken.error_code == "UNSUPPORTED_MEDIA" and broken.memory_retry is None
        assert heavy.error_code == "MCP_RESOURCE_LIMIT" and heavy.memory_retry is not None
        assert manager.allocated_memory_bytes == 0
    finally:
        manager.shutdown(wait=True)


def test_light_tasks_skip_free_memory_wait(monkeypatch):
    """整机内存紧张时，只读信息的轻任务和结果包读取仍可执行，重任务才等待。"""
    import pixelprobe_mcp.jobs as jobs
    monkeypatch.setattr(jobs, "system_available_memory_bytes", lambda: 0)
    assert jobs._system_has_room("capability", {"capability": "info", "media_path": "x"})
    assert jobs._system_has_room("read_artifact", {"bundle_path": "b"})
    assert not jobs._system_has_room("capability", {"capability": "scan", "media_path": "x"})


def test_memory_retry_waits_for_exclusive_budget(tmp_path, monkeypatch):
    """重试要等其他任务结束、独占整份预算，避免两个大任务同时重试再次一起超限。"""
    import pixelprobe_mcp.jobs as jobs
    release = threading.Event()
    other_started = threading.Event()
    seen: list[tuple[str, int, bool]] = []

    def execute(_config, _operation, payload, **kwargs):
        name = payload["capability"]
        seen.append((name, kwargs["memory_limit_bytes"], release.is_set()))
        if name == "pixel":
            other_started.set()
            release.wait(5)
            return {"ok": "other"}
        if len(seen) <= 2:
            other_started.wait(5)
            raise jobs.WorkerError("MCP_RESOURCE_LIMIT：工作进程实际内存超过预算，已停止执行")
        return {"ok": "retried"}

    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = JobManager(_memory_config(tmp_path))
    try:
        heavy = manager.submit("capability", {"capability": "scan", "media_path": "a.mkv"},
                               timeout_seconds=10, persistent=False)
        other = manager.submit("capability", {"capability": "pixel", "media_path": "b.mkv"},
                               timeout_seconds=10, persistent=False)
        time.sleep(0.5)
        assert len([item for item in seen if item[0] == "scan"]) == 1, "其他任务未结束前不得重试"
        release.set()
        heavy.future.result(10)
        other.future.result(10)
        assert heavy.result == {"ok": "retried"}
        assert seen[-1] == ("scan", 2048 * MIB, True)
    finally:
        release.set()
        manager.shutdown(wait=True)
