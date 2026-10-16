"""真实常驻工作进程的超时回收、复用、取消与数值一致性验证。"""
import multiprocessing
import threading

import pytest
from PIL import Image

from pixelprobe_mcp.config import ServerConfig
from pixelprobe_mcp.models import CapabilityInput
from pixelprobe_mcp.service import PixelProbeService
from pixelprobe_mcp.worker import WorkerError, _pool_for, run_operation_sync, shutdown_worker_pools


def _pixel(source, x=0, y=0) -> dict:
    return {"capability": "pixel", "media_path": str(source), "at": f"point({x},{y})", "images": False}


def _children() -> set[int]:
    return {process.pid for process in multiprocessing.active_children()}


def test_timeout_reaps_process_and_next_request_succeeds(tmp_path):
    """超时后不留下占用槽位的后台进程，随后真实像素请求仍可执行且与进程内结果一致。"""
    source = tmp_path / "像素.png"
    Image.new("RGB", (2, 2), (12, 34, 56)).save(source)
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    shutdown_worker_pools()
    before = _children()
    payload = _pixel(source)
    with pytest.raises(TimeoutError):
        run_operation_sync(config, "capability", payload, timeout_seconds=0.001)
    # 超时的工作进程必须被回收，不能留在空闲池里复用
    assert _children() == before
    envelope, images = run_operation_sync(config, "capability", payload, timeout_seconds=60)
    expected, _ = PixelProbeService(config).run_capability(CapabilityInput.model_validate(payload))
    assert envelope.model_dump() == expected.model_dump()
    assert images == []
    # 正常完成的进程可以留作空闲复用；关闭进程池后不得残留任何子进程
    assert len(_children() - before) <= 1
    shutdown_worker_pools()
    assert _children() == before


def test_pool_reuses_one_worker_for_consecutive_requests(tmp_path):
    """连续请求复用同一个常驻进程，不再逐次启动新进程。"""
    source = tmp_path / "复用.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(source)
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    shutdown_worker_pools()
    pool = _pool_for(config)
    spawned_before = pool.spawned
    for _ in range(4):
        envelope, _ = run_operation_sync(config, "capability", _pixel(source, 1, 1), timeout_seconds=60)
        assert envelope.data["pixels"][0]["rgb"] == {"r": 1, "g": 2, "b": 3}
    assert pool.spawned - spawned_before == 1
    assert pool.idle_count == 1
    shutdown_worker_pools()
    assert pool.idle_count == 0


def test_small_task_after_large_task_is_not_charged_for_previous_peak(tmp_path):
    """回归：Windows 的 peak_wset 是进程终身峰值，大任务之后复用进程的小任务不能被误判超限。"""
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (2000, 1500), (40, 80, 120)).save(photo, quality=90)
    small = tmp_path / "small.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(small)
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    shutdown_worker_pools()
    try:
        run_operation_sync(config, "capability", {
            "capability": "evidence", "media_path": str(photo), "output_name": "big.bundle",
        }, timeout_seconds=120)
        for _ in range(2):
            envelope, _ = run_operation_sync(config, "capability", _pixel(small, 1, 1), timeout_seconds=60)
            assert envelope.data["pixels"][0]["rgb"] == {"r": 1, "g": 2, "b": 3}
    finally:
        shutdown_worker_pools()


def test_cancelled_worker_is_retired_not_reused(tmp_path):
    source = tmp_path / "取消.png"
    Image.new("RGB", (4, 4), (9, 9, 9)).save(source)
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    shutdown_worker_pools()
    pool = _pool_for(config)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(WorkerError, match="MCP_CANCELLED"):
        run_operation_sync(config, "capability", _pixel(source), timeout_seconds=10, cancel_event=cancel)
    assert pool.idle_count == 0
    envelope, _ = run_operation_sync(config, "capability", _pixel(source), timeout_seconds=10)
    assert envelope.data["pixels"][0]["rgb"] == {"r": 9, "g": 9, "b": 9}
    assert pool.idle_count == 1
    shutdown_worker_pools()


def test_worker_errors_do_not_echo_untrusted_payload(tmp_path):
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    secret = str(tmp_path / "private-secret.png")
    corrupt = tmp_path / "private-secret-corrupt.png"
    corrupt.write_bytes(b"not an image")
    (tmp_path / "private-secret-empty.png").write_bytes(b"")
    for payload in (
        {"capability": "pixel", "media_path": secret, "at": "point(-1,0)"},
        {"capability": "pixel", "media_path": "private-secret", "unknown": 1},
        {"capability": "no-such-capability-private-secret", "media_path": secret},
        {"capability": "pixel", "media_path": str(corrupt), "at": "point(0,0)"},
        {"capability": "pixel", "media_path": str(tmp_path / "private-secret-empty.png"), "at": "point(0,0)"},
    ):
        with pytest.raises(WorkerError) as caught:
            run_operation_sync(config, "capability", payload, timeout_seconds=30)
        assert "private-secret" not in str(caught.value)
    shutdown_worker_pools()
