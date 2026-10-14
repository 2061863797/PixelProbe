"""真实 spawn 进程的超时回收与数值兼容验证。"""
import multiprocessing
import threading

import pytest
from PIL import Image
import anyio
from pixelprobe_mcp.worker import (
    WorkerError,
    _pool_for,
    run_operation_sync,
    run_read,
    shutdown_worker_pools,
)
from mcp.server.fastmcp.exceptions import ToolError

from pixelprobe_mcp.config import ServerConfig
from pixelprobe_mcp.service import PixelProbeService
from pixelprobe_mcp.models import PixelInput
import pixelprobe_mcp.server as server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_timeout_reaps_process_and_next_request_succeeds(tmp_path, monkeypatch):
    """客户端超时后不留下占用槽位的后台进程，随后真实像素请求仍可执行。"""
    source = tmp_path / "像素.png"
    Image.new("RGB", (2, 2), (12, 34, 56)).save(source)
    service = PixelProbeService(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    monkeypatch.setattr(server, "SERVICE", service)
    shutdown_worker_pools()
    before = {process.pid for process in multiprocessing.active_children()}
    payload = {"media_path": str(source), "points": [{"x": 0, "y": 0}]}
    with pytest.raises(ToolError, match="MCP_TIMEOUT"):
        await server._run_model(service.read_pixels, PixelInput, payload, timeout_seconds=0.001)
    # 超时的工作进程必须被回收，不能留在空闲池里复用
    assert {process.pid for process in multiprocessing.active_children()} == before
    result = await server._run_model(service.read_pixels, PixelInput, payload)
    expected = service.read_pixels(PixelInput.model_validate(payload))
    assert result.model_dump() == expected.model_dump()
    # 正常完成的进程可以留作空闲复用；关闭进程池后不得残留任何子进程
    assert len({process.pid for process in multiprocessing.active_children()} - before) <= 1
    shutdown_worker_pools()
    assert {process.pid for process in multiprocessing.active_children()} == before


@pytest.mark.anyio
async def test_pool_reuses_one_worker_for_consecutive_requests(tmp_path, monkeypatch):
    """连续请求复用同一个常驻进程，不再逐次启动新进程。"""
    source = tmp_path / "复用.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(source)
    service = PixelProbeService(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    monkeypatch.setattr(server, "SERVICE", service)
    shutdown_worker_pools()
    payload = {"media_path": str(source), "points": [{"x": 1, "y": 1}]}
    pool = _pool_for(service.config)
    spawned_before = pool.spawned
    for _ in range(4):
        result = await server._run_model(service.read_pixels, PixelInput, payload)
        assert result.data["pixels"][0]["rgb"] == {"r": 1, "g": 2, "b": 3}
    assert pool.spawned - spawned_before == 1
    assert pool.idle_count == 1
    shutdown_worker_pools()
    assert pool.idle_count == 0


@pytest.mark.anyio
async def test_cancelled_worker_is_retired_not_reused(tmp_path, monkeypatch):
    source = tmp_path / "取消.png"
    Image.new("RGB", (4, 4), (9, 9, 9)).save(source)
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    shutdown_worker_pools()
    pool = _pool_for(config)
    cancel = threading.Event()
    cancel.set()
    payload = {"media_path": str(source), "points": [{"x": 0, "y": 0}]}
    with pytest.raises(WorkerError, match="MCP_CANCELLED"):
        run_operation_sync(config, "read_pixels", payload, timeout_seconds=10,
                           cancel_event=cancel)
    assert pool.idle_count == 0
    result = run_operation_sync(config, "read_pixels", payload, timeout_seconds=10)
    assert result.data["pixels"][0]["rgb"] == {"r": 9, "g": 9, "b": 9}
    assert pool.idle_count == 1
    shutdown_worker_pools()


@pytest.mark.anyio
async def test_worker_validation_does_not_echo_untrusted_payload(tmp_path, monkeypatch):
    service = PixelProbeService(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    monkeypatch.setattr(server, "SERVICE", service)
    with pytest.raises(WorkerError) as caught:
        await run_read(service.config, "read_pixels", {
            "media_path": "private-secret", "points": [{"x": -1, "y": 0}],
        })
    assert "private-secret" not in str(caught.value)
