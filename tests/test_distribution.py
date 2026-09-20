"""通过真实 CLI/MCP 进程验证源码、冻结程序或 uvx 发行入口。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import av
import numpy as np
import pytest
import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from test_c2pa_real import signed_asset  # noqa: F401
from pixelprobe.artifacts import BundleReader


@pytest.fixture
def anyio_backend():
    return "asyncio"


def command():
    if value := os.environ.get("PIXELPROBE_TEST_COMMAND"):
        result = json.loads(value)
        assert isinstance(result, list) and result and all(isinstance(x, str) for x in result)
        return result
    if executable := os.environ.get("PIXELPROBE_TEST_EXECUTABLE"):
        return [executable]
    return [sys.executable, "-m", "pixelprobe"]


def environment(root):
    env = dict(os.environ)
    env["PIXELPROBE_MCP_ROOTS"] = str(root)
    env["PIXELPROBE_MCP_ARTIFACT_ROOT"] = str(root / "结果")
    env["PIXELPROBE_MCP_WORKERS"] = "1"
    return env


def request(representation="frames", storage="zarr"):
    return {
        "source": {"source_id": "source_main", "kind": "file", "uri": "overridden"},
        "selection": {"mode": "indices", "requested_indices": [0, 1]},
        "representation": representation,
        "feature": {"name": "farneback" if representation == "feature_t" else "rgb"},
        "output": {"format": storage, "include_preview": False},
    }


@pytest.mark.anyio
async def test_distribution_forced_timeout_reaps_suspended_worker(tmp_path):
    """挂起本测试启动的 worker，证明不能协作退出时由服务强制回收。"""
    source = tmp_path / "超时恢复.png"
    Image.new("RGB", (2, 2), (11, 22, 33)).save(source)
    cmd = command()
    params = StdioServerParameters(command=cmd[0], args=[*cmd[1:], "--mcp"], env=environment(tmp_path))
    suspended = []
    payload = {"media_path": str(source), "points": [{"x": 0, "y": 0}]}
    try:
        with anyio.fail_after(50):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    owner = psutil.Process()
                    baseline = {p.pid for p in owner.children(recursive=True)}
                    replies = []

                    async def read():
                        replies.append(await session.call_tool("pixelprobe_read_pixels", payload))

                    async with anyio.create_task_group() as group:
                        group.start_soon(read)
                        with anyio.fail_after(8):
                            while not suspended:
                                assert not replies, "worker 在故障注入前已完成，未覆盖强制回收"
                                for child in owner.children(recursive=True):
                                    try:
                                        if child.pid not in baseline and "--multiprocessing-fork" in child.cmdline():
                                            child.suspend()
                                            suspended.append(child)
                                    except psutil.NoSuchProcess:
                                        continue
                                await anyio.sleep(0.005)
                    assert replies[0].isError
                    assert "MCP_TIMEOUT" in str(replies[0].content)
                    # 断言前不恢复或杀死进程；必须由产品完成回收。
                    assert all(not p.is_running() for p in suspended)
                    recovered = await call(session, "pixelprobe_read_pixels", payload)
                    assert recovered["pixels"][0]["stored_sample"] == [11, 22, 33]
    finally:
        # 仅清理本测试已确认并挂起的子进程，异常时也不留下挂起 worker。
        for child in suspended:
            try:
                if child.is_running():
                    child.kill()
                    child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


async def call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent["data"]


def test_distribution_cli_zarr_roundtrip(tmp_path):
    # 完整版验收缺少 Zarr 时必须失败，不允许静默跳过。
    import zarr  # noqa: F401
    source = tmp_path / "中文图片.png"
    Image.new("RGB", (8, 8), (7, 80, 129)).save(source)
    spec = request()
    spec["selection"] = {"mode": "all"}
    request_file = tmp_path / "请求.json"
    request_file.write_text(json.dumps(spec), encoding="utf-8")
    target = tmp_path / "分块结果.bundle"
    proc = subprocess.run(
        [*command(), "generate", str(source), "--request", str(request_file),
         "--output", str(target), "--json"], capture_output=True, timeout=90,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["success"]
    bundle = BundleReader().open(target, verify="full")
    record = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    assert record.storage.format == "zarr"
    tensor = bundle.open_tensor(record.artifact_id)
    try:
        np.testing.assert_array_equal(tensor.data.materialize(), np.full((1, 8, 8, 3), [7, 80, 129], dtype=np.uint8))
    finally:
        close = getattr(tensor.data, "close", None)
        if callable(close):
            close()
    verified = subprocess.run(
        [*command(), "validate", str(target), "--json"], capture_output=True, timeout=60,
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["data"]["content_integrity_verified"]


@pytest.mark.anyio
async def test_distribution_mcp_flow_zarr_and_signed_evidence(tmp_path, signed_asset):
    target, _, anchors = signed_asset
    video = tmp_path / "静止视频.mkv"
    with av.open(str(video), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width = stream.height = 32
        stream.pix_fmt = "bgr0"
        for _ in range(2):
            frame = av.VideoFrame.from_ndarray(np.zeros((32, 32, 3), dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    cmd = command()
    params = StdioServerParameters(command=cmd[0], args=[*cmd[1:], "--mcp"], env=environment(tmp_path))
    with anyio.fail_after(120):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                generated = await call(session, "pixelprobe_generate_representation", {
                    "media_path": str(video), "request": request("feature_t"), "output_name": "flow.bundle",
                })
                bundle = BundleReader().open(Path(generated["bundle_path"]), verify="full")
                artifact_id = next(item.artifact_id for item in bundle.manifest.artifacts
                                   if item.kind == "data" and item.storage.shape == (32, 32, 2))
                assert bundle.artifact(artifact_id).storage.format == "zarr"
                values = await call(session, "pixelprobe_read_artifact", {
                    "bundle_path": generated["bundle_path"], "artifact_id": artifact_id,
                    "selection": [{"start": 0, "stop": 32}, {"start": 0, "stop": 32}, {"start": 0, "stop": 2}],
                    "max_values": 2048, "verify": "full",
                })
                assert values["selected_shape"] == [32, 32, 2]
                np.testing.assert_allclose(values["values"], 0, atol=1e-6)
                evidence = await call(session, "pixelprobe_evidence", {
                    "media_path": str(target), "c2pa_trust_anchors_pem": anchors,
                    "output_name": "evidence.bundle",
                })
                assert evidence["c2pa"]["verification_state"] == "trusted"
                assert evidence["c2pa"]["signature_validated"] is True
                BundleReader().open(tmp_path / "结果" / "evidence.bundle", verify="full")


@pytest.mark.anyio
async def test_distribution_running_cancel_and_recovery(tmp_path):
    video = tmp_path / "长视频.mkv"
    # 大于启动与轮询时间的真实解码工作量；不向产品注入测试专用阻塞入口。
    pixels = np.random.default_rng(17).integers(0, 256, (256, 256, 3), dtype=np.uint8)
    with av.open(str(video), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width = stream.height = 256
        stream.pix_fmt = "bgr0"
        for _ in range(400):
            for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    cmd = command()
    params = StdioServerParameters(command=cmd[0], args=[*cmd[1:], "--mcp"], env=environment(tmp_path))
    with anyio.fail_after(60):
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                job = await call(session, "pixelprobe_submit_job", {
                    "operation": "workflow", "payload": {"media_path": str(video),
                    "options": {"workflow": "quality_overview", "end_frame": 399},
                    "output_name": "cancelled.bundle"},
                })
                job_id = job["job_id"]
                while True:
                    state = await call(session, "pixelprobe_get_job", {"job_id": job_id})
                    assert state["status"] in {"queued", "running"}, state
                    if state["status"] == "running" and state["resource_plan"] is not None:
                        break
                    await anyio.sleep(0.02)
                await call(session, "pixelprobe_cancel_job", {"job_id": job_id})
                while True:
                    state = await call(session, "pixelprobe_get_job", {"job_id": job_id})
                    if state["status"] == "cancelled":
                        break
                    assert state["status"] == "running", state
                    await anyio.sleep(0.02)
                assert not state["result_ready"]
                refused = await session.call_tool("pixelprobe_get_job_result", {"job_id": job_id})
                assert refused.isError
                assert not (tmp_path / "结果" / "cancelled.bundle").exists()
                recovered = await call(session, "pixelprobe_read_pixels", {
                    "media_path": str(video), "frame": 0, "points": [{"x": 0, "y": 0}],
                })
                rgb = recovered["pixels"][0]["rgb"]
                assert [rgb["r"], rgb["g"], rgb["b"]] == pixels[0, 0].tolist()
