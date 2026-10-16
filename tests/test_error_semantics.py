"""错误语义回归：输出已存在、坏文件、未知 artifact、领取不会有结果的任务、索引改名失败。"""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pixelprobe.artifacts.errors import ArtifactNotFoundError, BundleTargetExistsError
from pixelprobe.core.media_reader import get_media_info
from pixelprobe.models.errors import MediaNotFoundError, OutputWriteError, UnsupportedMediaError
from pixelprobe_mcp.config import PathAccessError, ServerConfig


def _config(tmp_path: Path) -> ServerConfig:
    return ServerConfig((tmp_path.resolve(),), (tmp_path / "artifacts").resolve())


def test_existing_output_name_reports_bundle_target_exists_not_path_error(tmp_path):
    config = _config(tmp_path)
    (config.prepare_artifact_root() / "dup.bundle").mkdir()
    with pytest.raises(BundleTargetExistsError) as caught:
        config.prepare_artifact_target("dup.bundle")
    assert not isinstance(caught.value, PathAccessError)
    assert caught.value.code == "BUNDLE_TARGET_EXISTS"


def test_missing_file_inside_allowlist_reports_file_not_found(tmp_path):
    with pytest.raises(MediaNotFoundError) as caught:
        _config(tmp_path).resolve_file(str(tmp_path / "缺失.png"))
    # 只回显文件名，不回显完整路径
    assert str(tmp_path) not in caught.value.message


def test_missing_file_outside_allowlist_still_reports_path_not_allowed(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    with pytest.raises(PathAccessError):
        _config(inner).resolve_file(str(tmp_path / "外部不存在.png"))


@pytest.mark.parametrize("content", [b"", b"not an image at all"])
def test_empty_or_corrupt_image_reports_unsupported_media(tmp_path, content):
    path = tmp_path / "broken.png"
    path.write_bytes(content)
    with pytest.raises(UnsupportedMediaError) as caught:
        get_media_info(path)
    assert str(tmp_path) not in caught.value.message


def test_unknown_artifact_reports_artifact_not_found(tmp_path):
    from pixelprobe.artifacts import BundleReader
    from pixelprobe.evidence import analyze_evidence
    source = tmp_path / "tiny.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(source)
    analyze_evidence(source, output_path=tmp_path / "e.bundle")
    bundle = BundleReader().open(tmp_path / "e.bundle")
    with pytest.raises(ArtifactNotFoundError) as caught:
        bundle.open_tensor("nope")
    assert caught.value.code == "ARTIFACT_NOT_FOUND"
    # 兼容旧的字典查找语义
    assert isinstance(caught.value, KeyError)


def test_claiming_cancelled_job_says_it_will_never_have_a_result(tmp_path, monkeypatch):
    import pixelprobe_mcp.jobs as jobs
    manager = jobs.JobManager(_config(tmp_path))
    try:
        job = jobs.Job("0" * 32, "capability", {}, 5, False)
        job.status = "cancelled"
        manager._jobs[job.job_id] = job
        with pytest.raises(jobs.WorkerError, match="MCP_JOB_NOT_READY：任务状态为 cancelled，不会产生结果"):
            manager.result(job.job_id)
    finally:
        manager.shutdown(wait=True)


def test_mcp_no_index_skips_index_lookup(tmp_path, monkeypatch):
    """L5：MCP 与命令行 --no-index 对等，可强制逐帧解码。"""
    from pixelprobe.session import SessionCache
    from pixelprobe_mcp.models import CapabilityInput
    from pixelprobe_mcp.service import PixelProbeService
    seen = []
    monkeypatch.setattr(SessionCache, "index_for", lambda self, media: seen.append(media))
    service = PixelProbeService(_config(tmp_path))
    source = tmp_path / "v.mkv"
    import av
    with av.open(str(source), "w") as container:
        stream = container.add_stream("ffv1", rate=10)
        stream.width = stream.height = 16
        stream.pix_fmt = "bgr0"
        for _ in range(3):
            frame = av.VideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    service.run_capability(CapabilityInput(capability="changes", media_path=str(source), images=False))
    assert len(seen) == 1
    service.run_capability(CapabilityInput(
        capability="changes", media_path=str(source), images=False, no_index=True,
    ))
    assert len(seen) == 1


@pytest.mark.parametrize("args", [
    ["index", "show", "x.mkv"], ["index", "remove", "x.mkv"], ["index", "prune"],
    ["generate", "x.mkv", "--request", "r.json", "--output", "o.bundle"],
])
def test_cli_accepts_no_progress_everywhere(args):
    """L4：脚本可对所有命令统一追加 --no-progress，不因个别命令不认识而报参数错误。"""
    from typer.testing import CliRunner
    from pixelprobe.cli.app import app
    result = CliRunner().invoke(app, [*args, "--no-progress", "--help"])
    assert result.exit_code == 0, result.output


def _built_index():
    from pixelprobe.index.builder import FRAME_FIELDS, BuiltIndex
    from pixelprobe.models.media_info import MediaInfo
    frames = {name: np.zeros(2, dtype=np.float64) for name in FRAME_FIELDS}
    frames["frame"] = np.arange(2)
    frames["time_seconds"] = np.array([0.0, 0.04])
    info = MediaInfo(path="v.mkv", media_type="video", width=4, height=4, channels=3,
                     color_mode="RGB", file_size_bytes=1)
    return BuiltIndex(info=info, frames=frames, segments=[], segment_threshold=0.0,
                      thumbs=np.zeros((2, 2, 2, 3), dtype=np.uint8),
                      thumb_frames=np.arange(2), thumb_stride=1)


def test_index_commit_falls_back_to_copy_when_rename_is_refused(tmp_path, monkeypatch):
    """回归：部分加密或重定向目录拒绝同目录改名（WinError 17），索引应退回复制提交。"""
    import pixelprobe.index.store as store
    media = tmp_path / "v.mkv"
    media.write_bytes(b"video-bytes")

    def refuse(*_args):
        error = OSError("不同的磁盘驱动器")
        error.winerror = 17
        raise error

    monkeypatch.setattr(store.os, "replace", refuse)
    index = store.IndexStore(tmp_path / "index").save(media, _built_index())
    assert index.frame_count == 2
    assert not [path for path in (tmp_path / "index").iterdir() if path.name.startswith(".tmp-")]


def test_index_write_failure_reports_actionable_error(tmp_path, monkeypatch):
    import pixelprobe.index.store as store
    media = tmp_path / "v.mkv"
    media.write_bytes(b"video-bytes")
    monkeypatch.setattr(store.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError(13, "拒绝访问")))
    with pytest.raises(OutputWriteError, match="索引写入失败") as caught:
        store.IndexStore(tmp_path / "index").save(media, _built_index())
    assert store.INDEX_DIR_ENV in caught.value.hint
