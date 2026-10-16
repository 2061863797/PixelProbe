"""视频容器与编码结构：关键帧、时间戳空隙、帧类型与线索。"""
from __future__ import annotations

from pathlib import Path

import pytest

from pixelprobe.address import parse_address
from pixelprobe.capabilities import capability
from pixelprobe.models.errors import UnsupportedMediaError
from test_find import _solid, _write_video


def _streams(media: Path, **options) -> dict:
    return capability("streams").invoke(media, parse_address(None), options).data


def test_intra_only_video_with_dropped_timestamps(tmp_path: Path) -> None:
    pts = [0, 40, 80, 120, 240, 280, 320, 360]
    video = _write_video(tmp_path / "gap.mkv", [_solid(10 * n) for n in range(8)], pts)
    data = _streams(video)
    packets = data["video_packets"]
    assert packets["packet_count"] == 8
    # PyAV 写 ffv1 缺省只把第一帧标为关键帧（GOP 覆盖整段）
    assert packets["keyframe_count"] == 1 and packets["gop_lengths"] == []
    assert packets["trailing_gop_length"] == 8 and packets["regular_gop"] is None
    assert packets["b_frames_inferred"] is False and packets["dts_regressions"] == 0
    assert packets["typical_frame_interval_seconds"] == pytest.approx(0.04)
    assert packets["timestamp_gaps"] == [{"after_seconds": 0.12, "interval_seconds": 0.12, "ratio": 3.0}]
    assert sum(packets["bytes_per_second"]["values"]) == packets["packet_size_bytes"]["count"] * \
        packets["packet_size_bytes"]["mean"]
    codes = {clue["code"] for clue in data["clues"]}
    assert {"MUXER_TAG", "TIMESTAMP_GAPS", "SINGLE_KEYFRAME"} <= codes
    assert "IRREGULAR_GOP" not in codes
    video_stream = data["streams"][data["video_stream_index"]]
    assert video_stream["codec"] == "ffv1" and video_stream["width"] == 32
    assert data["automatic_authenticity_conclusion"] is None and data["limitations"]
    assert data["frame_types"] is None


def test_h264_frame_types_match_packets(test_video: Path) -> None:
    data = _streams(test_video, frame_types=True)
    packets = data["video_packets"]
    counts = data["frame_types"]["counts"]
    assert sum(counts.values()) == packets["packet_count"] == 30
    assert counts.get("I", 0) >= packets["keyframe_count"] >= 1
    assert len(data["frame_types"]["sequence"]) == 30
    assert packets["keyframe_packet_positions"][0] == 0


def test_streams_rejects_images(test_image: Path) -> None:
    with pytest.raises(UnsupportedMediaError):
        _streams(test_image)


def test_cli_streams_json(test_video: Path) -> None:
    from conftest import run_json
    data = run_json("streams", str(test_video), "--json")["data"]
    assert data["video_packets"]["packet_count"] == 30
    assert data["container"]["format"].startswith("matroska")


def test_frame_type_decoding_gets_full_scan_timeout() -> None:
    """统计帧类型要顺序解码全片且不受帧数上限约束，时限须与建索引相同，不能用 60 秒标准时限。"""
    import pixelprobe_mcp.server as server
    assert server._timeout_for("streams") == server.MCP_STANDARD_TIMEOUT_SECONDS
    assert server._timeout_for("streams", {"frame_types": True}) == server.MCP_INDEX_TIMEOUT_SECONDS
    assert server._timeout_for("index_build") == server.MCP_INDEX_TIMEOUT_SECONDS


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_evidence_auto_selects_streams_for_video(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pixelprobe_mcp.server as server
    from pixelprobe_mcp.config import ServerConfig
    from pixelprobe_mcp.service import PixelProbeService
    target = tmp_path / "取证.mkv"
    target.write_bytes(test_video.read_bytes())
    monkeypatch.setattr(server, "SERVICE", PixelProbeService(ServerConfig((tmp_path,), tmp_path / "out")))
    monkeypatch.setattr(server, "_JOB_MANAGER", None)
    try:
        result = await server.pixelprobe_evidence(str(target))
        assert result.structuredContent["data"]["video_packets"]["packet_count"] == 30
        assert "不判定" in result.structuredContent["warnings"][0]
        assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.bundle"))
    finally:
        server._job_manager().shutdown(wait=True)
