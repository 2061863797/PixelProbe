"""受管存储预览、保留引用、互斥与安全清理。"""
import json
import os
import time
import threading
from datetime import datetime, timedelta, timezone

import pytest

from pixelprobe_mcp.config import ServerConfig
from pixelprobe_mcp.jobs import JobManager


def _manager(tmp_path):
    manager = JobManager(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    manager._ensure_store()
    return manager


def _record(manager, job_id, *, checkpoint=None, days=8, status="failed"):
    root = manager._ensure_store()
    value = {"job_id": job_id, "operation": "generate_representation",
             "status": status, "checkpoint_id": checkpoint, "result_ready": False,
             "finished_at": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()}
    (root / f"{job_id}.json").write_text(json.dumps(value), encoding="utf-8")
    (root / f"{job_id}.request.json").write_text("{}", encoding="utf-8")
    return root / f"{job_id}.json"


def _checkpoint(manager, checkpoint_id):
    root = manager.config.checkpoint_directory(checkpoint_id, create=True)
    (root / "cache").mkdir()
    (root / "cache" / "data.npy").write_bytes(b"cache")
    return root


def test_preview_and_cleanup_preserve_bundle_and_unknown_files(tmp_path):
    manager = _manager(tmp_path)
    try:
        job_id = "a" * 32
        record = _record(manager, job_id, checkpoint=job_id)
        checkpoint = _checkpoint(manager, job_id)
        bundle = manager.config.artifact_root / "formal.bundle"
        bundle.mkdir()
        (bundle / "data").write_bytes(b"formal")
        unknown = manager._root / "user.json"
        unknown.write_text("keep", encoding="utf-8")
        preview = manager.maintain_storage()
        assert preview["expired_jobs"] == preview["checkpoint_directories"] == 1
        assert preview["reclaimable_bytes"] > 5
        assert record.exists() and checkpoint.exists()
        result = manager.maintain_storage(dry_run=False)
        assert result["removed_jobs"] == result["removed_checkpoints"] == 1
        assert result["removed_bytes"] == preview["reclaimable_bytes"]
        assert not record.exists() and not checkpoint.exists()
        assert (bundle / "data").read_bytes() == b"formal"
        assert unknown.read_text("utf-8") == "keep"
    finally:
        manager.shutdown(wait=True)


def test_retained_resume_protects_shared_checkpoint(tmp_path):
    manager = _manager(tmp_path)
    try:
        original = "a" * 32
        _record(manager, original, checkpoint=original)
        retained = _record(manager, "b" * 32, checkpoint=original, days=1)
        checkpoint = _checkpoint(manager, original)
        result = manager.maintain_storage(dry_run=False)
        assert result["removed_jobs"] == 1
        assert result["removed_checkpoints"] == 0
        assert retained.exists() and checkpoint.exists()
    finally:
        manager.shutdown(wait=True)


def test_other_manager_blocks_cleanup_and_false_restart_recovery(tmp_path):
    first = _manager(tmp_path)
    second = None
    try:
        path = _record(first, "a" * 32, status="running")
        second = _manager(tmp_path)
        assert json.loads(path.read_text("utf-8"))["status"] == "running"
        assert second.maintain_storage(dry_run=False)["blocked"]
    finally:
        first.shutdown(wait=True)
        if second:
            second.shutdown(wait=True)


def test_active_worker_blocks_cleanup(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    entered, release = threading.Event(), threading.Event()
    def execute(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {}
    monkeypatch.setattr(jobs, "run_operation_sync", execute)
    manager = _manager(tmp_path)
    try:
        path = _record(manager, "a" * 32)
        job = manager.submit("read_pixels", {}, timeout_seconds=5, persistent=True)
        assert entered.wait(2)
        assert manager.maintain_storage(dry_run=False)["blocked"]
        assert path.exists()
        release.set()
        job.future.result(3)
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_checkpoint_delete_failure_preserves_record(tmp_path, monkeypatch):
    import pixelprobe_mcp.maintenance as maintenance
    manager = _manager(tmp_path)
    try:
        job_id = "a" * 32
        record = _record(manager, job_id, checkpoint=job_id)
        checkpoint = _checkpoint(manager, job_id)
        def fail(*args, **kwargs):
            raise PermissionError("locked")
        monkeypatch.setattr(maintenance.shutil, "rmtree", fail)
        report = manager.maintain_storage(dry_run=False)
        assert report["skipped"] and report["removed_jobs"] == 0
        assert record.exists() and checkpoint.exists()
    finally:
        manager.shutdown(wait=True)


def test_invalid_record_prevents_checkpoint_collection(tmp_path):
    manager = _manager(tmp_path)
    try:
        job_id = "a" * 32
        record = _record(manager, job_id, checkpoint=job_id)
        checkpoint = _checkpoint(manager, job_id)
        (manager._root / ("b" * 32 + ".json")).write_text("invalid", encoding="utf-8")
        report = manager.maintain_storage(dry_run=False)
        assert report["skipped"] and report["removed_checkpoints"] == 0
        assert checkpoint.exists()
        assert record.exists()
    finally:
        manager.shutdown(wait=True)


def test_reparse_checkpoint_is_preserved(tmp_path, monkeypatch):
    import pixelprobe_mcp.config as config
    manager = _manager(tmp_path)
    try:
        job_id = "a" * 32
        _record(manager, job_id, checkpoint=job_id)
        checkpoint = _checkpoint(manager, job_id)
        monkeypatch.setattr(config, "_is_reparse_point", lambda path: path == checkpoint)
        result = manager.maintain_storage(dry_run=False)
        assert result["removed_checkpoints"] == 0 and result["skipped"]
        assert checkpoint.exists()
    finally:
        manager.shutdown(wait=True)


def test_expiry_uses_finished_time_not_recent_mtime(tmp_path):
    manager = _manager(tmp_path)
    try:
        path = _record(manager, "a" * 32)
        assert path.exists()
        assert manager.maintain_storage()["expired_jobs"] == 1
        _record(manager, "b" * 32, days=0)
        assert manager.maintain_storage(dry_run=False)["removed_jobs"] == 1
        assert (manager._root / ("b" * 32 + ".json")).exists()
    finally:
        manager.shutdown(wait=True)


def test_cli_cache_clear_cannot_bypass_checkpoint_references(tmp_path):
    from pixelprobe.engine.cache import LocalArrayCache
    from pixelprobe.models.errors import InvalidRangeError
    root = tmp_path / ".checkpoints" / ("a" * 32) / "cache"
    cache = LocalArrayCache(root)
    data = cache.partials / "partial"
    data.mkdir()
    (data / "data.npy").write_bytes(b"preserve")
    with pytest.raises(InvalidRangeError, match="pixelprobe_maintain_storage"):
        cache.clear()
    assert (data / "data.npy").read_bytes() == b"preserve"


def test_owned_expired_orphan_is_collectible_unknown_or_recent_is_preserved(tmp_path):
    manager = _manager(tmp_path)
    try:
        owned = _checkpoint(manager, "a" * 32)
        old = time.time() - 9 * 86400
        for path in [*owned.rglob("*"), owned]:
            os.utime(path, (old, old))
        recent = _checkpoint(manager, "b" * 32)
        unknown = manager.config.artifact_root / ".checkpoints" / ("c" * 32)
        unknown.mkdir()
        (unknown / "user.txt").write_text("keep", encoding="utf-8")
        preview = manager.maintain_storage()
        assert preview["orphan_checkpoints"] == 1
        assert owned.exists()
        result = manager.maintain_storage(dry_run=False)
        assert result["removed_checkpoints"] == 1
        assert not owned.exists()
        assert recent.exists() and (unknown / "user.txt").exists()
    finally:
        manager.shutdown(wait=True)


def _hold_lease(path, ready, release):
    from pixelprobe_mcp.storage_lock import StorageLease
    with StorageLease(path):
        ready.set()
        release.wait(10)


def test_storage_lease_excludes_other_process_and_releases(tmp_path):
    import multiprocessing
    import sys
    from pixelprobe_mcp.storage_lock import StorageLease
    context = multiprocessing.get_context("spawn")
    process_type = context.Process
    if sys.platform == "win32":
        from pixelprobe_mcp.windows_spawn import HiddenSpawnProcess
        process_type = HiddenSpawnProcess
    ready, release = context.Event(), context.Event()
    path = tmp_path / "lease.lock"
    process = process_type(target=_hold_lease, args=(path, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(OSError):
            StorageLease(path)
        release.set()
        process.join(5)
        assert process.exitcode == 0
        with StorageLease(path):
            pass
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()
        process.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_storage_tool_defaults_to_preview(tmp_path, monkeypatch):
    import pixelprobe_mcp.server as server
    from pixelprobe_mcp.service import PixelProbeService
    from mcp.shared.memory import create_connected_server_and_client_session
    manager = _manager(tmp_path)
    record = _record(manager, "a" * 32)
    monkeypatch.setattr(server, "SERVICE", PixelProbeService(manager.config))
    monkeypatch.setattr(server, "_JOB_MANAGER", manager)
    try:
        async with create_connected_server_and_client_session(server.mcp, raise_exceptions=True) as session:
            preview = await session.call_tool("pixelprobe_maintain_storage", {})
            assert not preview.isError
            assert preview.structuredContent["data"]["dry_run"]
            assert record.exists()
            result = await session.call_tool("pixelprobe_maintain_storage", {"dry_run": False})
            assert not result.isError
            assert result.structuredContent["data"]["removed_jobs"] == 1
            assert not record.exists()
    finally:
        manager.shutdown(wait=True)
