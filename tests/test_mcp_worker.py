"""真实 spawn 进程的超时回收与数值兼容验证。"""
import multiprocessing

import pytest
from PIL import Image
import anyio
from pixelprobe_mcp.worker import run_read, WorkerError
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
    before = {process.pid for process in multiprocessing.active_children()}
    payload = {"media_path": str(source), "points": [{"x": 0, "y": 0}]}
    with pytest.raises(ToolError, match="MCP_TIMEOUT"):
        await server._run_model(service.read_pixels, PixelInput, payload, timeout_seconds=0.001)
    assert {process.pid for process in multiprocessing.active_children()} == before
    result = await server._run_model(service.read_pixels, PixelInput, payload)
    expected = service.read_pixels(PixelInput.model_validate(payload))
    assert result.model_dump() == expected.model_dump()
    assert {process.pid for process in multiprocessing.active_children()} == before


@pytest.mark.anyio
async def test_worker_validation_does_not_echo_untrusted_payload(tmp_path, monkeypatch):
    service = PixelProbeService(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    monkeypatch.setattr(server, "SERVICE", service)
    with pytest.raises(WorkerError) as caught:
        await run_read(service.config, "read_pixels", {
            "media_path": "private-secret", "points": [{"x": -1, "y": 0}],
        })
    assert "private-secret" not in str(caught.value)
