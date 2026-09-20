from fractions import Fraction
import json

import av
import numpy as np
from PIL import Image
import pytest

from pixelprobe.artifacts import BundleReader
from pixelprobe.workflows import WorkflowOptions, run_workflow


def video(path, levels, pts=None):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("ffv1", rate=25)
        stream.width = stream.height = 16
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, 1000)
        for index, level in enumerate(levels):
            frame = av.VideoFrame.from_ndarray(np.full((16, 16, 3), level, dtype=np.uint8), format="rgb24")
            frame.pts = pts[index] if pts else index * 40
            frame.time_base = Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def tensor(result, role):
    bundle = BundleReader().open(result.bundle.root, verify="full")
    record = next(a for a in bundle.manifest.artifacts if a.role == role)
    return bundle.open_tensor(record.artifact_id)


def test_first_change_stops_and_preserves_numeric_artifacts(tmp_path):
    source = video(tmp_path / "变化.mkv", [0] * 3 + [255] * 20)
    result = run_workflow(source, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="first_change", end_frame=22))
    assert result.report["first_change"]["frame"] == 3
    assert result.report["first_change"]["reference_frame"] == 2
    assert result.report["first_change"]["changed_bbox"] == [0, 0, 16, 16]
    assert result.report["records"] == 4
    assert result.report["coverage"][0]["source_frames_decoded"] == 5
    metrics = tensor(result, "workflow_metrics")
    assert np.array_equal(metrics.validity.materialize()[0, 2:7], [False] * 5)
    assert metrics.data.materialize()[3, 4] == 255
    difference = tensor(result, "workflow_difference")
    assert np.all(difference.data.materialize() == 255)
    bundle = BundleReader().open(result.bundle.root, verify="full")
    preview = next(a for a in bundle.manifest.artifacts if a.kind == "preview")
    schema = json.loads((bundle.root / bundle.artifact(preview.tensor_schema_artifact_id).storage.files[0].uri).read_text("utf-8"))
    assert schema["descriptor"]["attributes"]["dag_input_node_ids"] == ["workflow.difference"]


def test_sampling_cannot_claim_absence_of_transient_change(tmp_path):
    source = video(tmp_path / "flash.mkv", [0, 255, 0, 0, 0, 0, 0])
    result = run_workflow(source, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="first_change", end_frame=6, sample_every=3))
    assert result.report["first_change"] is None
    assert result.report["coverage"][0]["sampling_gaps"]
    assert result.report["records"] == 3
    assert result.report["coverage"][0]["frames_converted"] == 3
    assert result.report["coverage"][0]["source_frames_decoded"] == 7


def test_quality_overview_exact_counts_and_eof(tmp_path):
    source = video(tmp_path / "quality.mkv", [0] * 3 + [255] * 5)
    result = run_workflow(source, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="quality_overview", end_frame=99))
    assert result.report["quality_candidates"] == {
        "black_candidates": 3, "white_candidates": 5, "flat_candidates": 8,
    }
    assert result.report["coverage"][0]["eof_confirmed"]
    assert result.report["comparison_pairs"] == 7
    assert result.report["automatic_authenticity_conclusion"] is None


def test_media_comparison_exact_pixels_and_original_coordinates(tmp_path):
    a = np.zeros((8, 8, 3), dtype=np.uint8)
    b = a.copy()
    b[2, 3] = 30
    one, two = tmp_path / "一.png", tmp_path / "二.png"
    Image.fromarray(a).save(one)
    Image.fromarray(b).save(two)
    result = run_workflow(one, reference_path=two, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="compare_media"))
    pair = result.report["first_change"]
    assert pair["changed_ratio"] == 1 / 64
    assert pair["mean_abs_difference"] == 30 / 64
    assert pair["changed_bbox"] == [3, 2, 1, 1]
    assert result.report["comparison_pairs"] == 1
    assert len(result.bundle.manifest.sources) == 2


def test_unpaired_tail_is_explicit(tmp_path):
    one = video(tmp_path / "long.mkv", [0] * 8)
    two = video(tmp_path / "short.mkv", [0] * 4)
    result = run_workflow(one, reference_path=two, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="compare_media", end_frame=99))
    assert result.report["stop_reason"] == "unpaired_sample"
    assert result.report["comparison_pairs"] == 4
    assert result.report["coverage"][0]["selected_frames_returned"] == 5
    assert result.report["coverage"][0]["last_analyzed_frame"] == 3
    assert not result.report["coverage"][0]["eof_confirmed"]
    assert result.report["coverage"][1]["eof_confirmed"]


def test_duplicate_pts_preserves_presentation_identity(tmp_path):
    source = video(tmp_path / "重复时间.mkv", [0, 0, 255, 255], pts=[0, 40, 40, 120])
    result = run_workflow(source, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="first_change", end_frame=3))
    assert result.report["first_change"]["frame"] == 2
    assert result.report["first_change"]["time_seconds"] == 0.04
    assert result.report["coverage"][0]["timestamp_flags"]["PTS_DUPLICATE"] == 1


def test_comparison_reports_vfr_time_misalignment_without_resampling(tmp_path):
    one = video(tmp_path / "one.mkv", [0, 20, 40, 60], pts=[0, 40, 120, 240])
    two = video(tmp_path / "two.mkv", [0, 20, 40, 60], pts=[0, 80, 160, 320])
    result = run_workflow(one, reference_path=two, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="compare_media", end_frame=3))
    metrics = tensor(result, "workflow_metrics").data.materialize()
    assert np.allclose(metrics[:, 1], [0, .04, .12, .24])
    assert np.allclose(metrics[:, 3], [0, .08, .16, .32])
    assert np.all(metrics[:, 4] == 0)
    assert result.report["comparison_alignment"] == "presentation_index"
    assert result.report["max_pair_time_delta_seconds"] == pytest.approx(.08)


def test_estimated_frame_count_does_not_truncate_workflow(tmp_path, monkeypatch):
    import pixelprobe.workflows as workflows
    source = video(tmp_path / "tail.mkv", [0] * 7 + [255])
    original = workflows.get_media_info
    monkeypatch.setattr(workflows, "get_media_info", lambda p: original(p).model_copy(
        update={"frame_count": 2, "frame_count_estimated": True}))
    result = run_workflow(source, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="first_change", end_frame=7))
    assert result.report["first_change"]["frame"] == 7
    assert result.report["coverage"][0]["source_frames_decoded"] == 8


def test_mismatch_and_missing_reference_rejected(tmp_path):
    one, two = tmp_path / "one.png", tmp_path / "two.png"
    Image.new("RGB", (8, 8)).save(one)
    Image.new("RGB", (9, 8)).save(two)
    with pytest.raises(ValueError):
        WorkflowOptions(workflow="quality_overview", end_frame=10000)
    from pixelprobe.models.errors import InvalidRangeError
    with pytest.raises(InvalidRangeError):
        run_workflow(one, output_path=tmp_path / "missing.bundle", options=WorkflowOptions(workflow="compare_media"))
    with pytest.raises(InvalidRangeError):
        run_workflow(one, reference_path=two, output_path=tmp_path / "bad.bundle",
                      options=WorkflowOptions(workflow="compare_media"))
    assert not (tmp_path / "bad.bundle").exists()


def test_workflow_cli(tmp_path):
    from conftest import run_json
    source = tmp_path / "图片.png"
    Image.new("RGB", (8, 8), "white").save(source)
    result = run_json("workflow", str(source), "--mode", "quality_overview", "--json")
    assert result["success"]
    assert result["data"]["records"] == 1


def test_workflow_spawn_and_two_input_plan(tmp_path):
    from pixelprobe_mcp.config import ServerConfig
    from pixelprobe_mcp.worker import run_operation_sync
    from pixelprobe_mcp.resource_plan import plan_operation
    one, two = tmp_path / "one.png", tmp_path / "two.png"
    Image.new("RGB", (8, 8), "black").save(one)
    Image.new("RGB", (8, 8), "white").save(two)
    config = ServerConfig((tmp_path,), tmp_path / "artifacts")
    payload = {"media_path": str(one), "reference_path": str(two),
               "options": {"workflow": "compare_media"}}
    plan = plan_operation(config, "workflow", payload)
    assert plan["source_count"] == 2
    assert plan["decoded_frames_upper_bound"] == 2
    result = run_operation_sync(config, "workflow", payload, timeout_seconds=30)
    assert result.data["first_change"]["changed_ratio"] == 1


def test_reference_path_has_independent_allowlist_check(tmp_path):
    from pixelprobe_mcp.config import ServerConfig, PathAccessError
    from pixelprobe_mcp.resource_plan import plan_operation
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    one, two = allowed / "one.png", tmp_path / "outside.png"
    Image.new("RGB", (8, 8)).save(one)
    Image.new("RGB", (8, 8)).save(two)
    config = ServerConfig((allowed,), allowed / "artifacts")
    with pytest.raises(PathAccessError):
        plan_operation(config, "workflow", {
            "media_path": str(one), "reference_path": str(two),
            "options": {"workflow": "compare_media"},
        })
    assert not config.artifact_root.exists()


@pytest.mark.parametrize("which", ["main", "reference"])
def test_changed_source_rejects_bundle(tmp_path, monkeypatch, which):
    import pixelprobe.workflows as workflows
    from pixelprobe.domain.errors import MediaChangedDuringAnalysisError
    one, two = tmp_path / "one.png", tmp_path / "two.png"
    Image.new("RGB", (8, 8)).save(one)
    Image.new("RGB", (8, 8)).save(two)
    original = workflows.absolute_difference
    def changed(*args):
        result = original(*args)
        Image.new("RGB", (8, 8), "red").save(one if which == "main" else two)
        return result
    monkeypatch.setattr(workflows, "absolute_difference", changed)
    with pytest.raises(MediaChangedDuringAnalysisError):
        run_workflow(one, reference_path=two, output_path=tmp_path / "result.bundle",
                      options=WorkflowOptions(workflow="compare_media"))
    assert not (tmp_path / "result.bundle").exists()


def test_cancel_during_comparison_does_not_commit(tmp_path, monkeypatch):
    import pixelprobe.workflows as workflows
    from pixelprobe.engine.execution import CancellationToken
    from pixelprobe.engine.errors import ExecutionCancelledError
    token = CancellationToken()
    source = video(tmp_path / "video.mkv", [0, 255, 255])
    original = workflows.absolute_difference
    def cancel(*args):
        result = original(*args)
        token.cancel()
        return result
    monkeypatch.setattr(workflows, "absolute_difference", cancel)
    with pytest.raises(ExecutionCancelledError):
        run_workflow(source, output_path=tmp_path / "result.bundle",
                      options=WorkflowOptions(workflow="first_change"), cancellation=token)
    assert not (tmp_path / "result.bundle").exists()


def test_disk_budget_failure_leaves_no_bundle(tmp_path):
    from pixelprobe.operators.base import ResourcePolicy
    from pixelprobe.engine.errors import ResourcePlanUnsatisfiableError
    source = tmp_path / "image.png"
    Image.new("RGB", (8, 8)).save(source)
    with pytest.raises(ResourcePlanUnsatisfiableError):
        run_workflow(source, output_path=tmp_path / "result.bundle",
                      options=WorkflowOptions(workflow="quality_overview"),
                      resources=ResourcePolicy(max_memory_bytes=256 * 1024**2, max_temporary_bytes=1))
    assert not (tmp_path / "result.bundle").exists()
    assert not list(tmp_path.glob(".result.bundle.tmp-*"))


def test_memory_preflight_before_image_decode(tmp_path, monkeypatch):
    from pixelprobe.operators.base import ResourcePolicy
    from pixelprobe.domain.errors import MaterializationLimitExceededError
    from PIL.PngImagePlugin import PngImageFile
    source = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(source)
    monkeypatch.setattr(PngImageFile, "load", lambda *a, **k: pytest.fail("不应完整解码"))
    with pytest.raises(MaterializationLimitExceededError):
        run_workflow(source, output_path=tmp_path / "result.bundle",
                      options=WorkflowOptions(workflow="quality_overview"),
                      resources=ResourcePolicy(max_memory_bytes=1024))


def test_persistent_workflow_result_is_claimable(tmp_path):
    from pixelprobe_mcp.config import ServerConfig
    from pixelprobe_mcp.jobs import JobManager
    source = tmp_path / "image.png"
    Image.new("RGB", (8, 8)).save(source)
    manager = JobManager(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    try:
        job = manager.submit("workflow", {"media_path": str(source),
            "options": {"workflow": "quality_overview"}}, timeout_seconds=30, persistent=True)
        job.future.result(35)
        result, image = manager.result(job.job_id)
        assert result["data"]["records"] == 1
        assert image is None
        assert job.status == "succeeded"
        assert job.resource_plan["estimate_kind"] == "bounded_streaming_workflow"
    finally:
        manager.shutdown(wait=True)


def test_first_candidate_preview_uses_candidate_not_earlier_higher_mean(tmp_path, monkeypatch):
    import pixelprobe.workflows as workflows
    # 首对只有一个强变化像素；后对所有像素变化 1。面积阈值应选择后对。
    a = np.zeros((16, 16, 3), dtype=np.uint8)
    b = a.copy()
    b[0, 0] = 255
    c = b.copy()
    c[1:] = 1
    arrays = [a, b, c]
    source = video(tmp_path / "video.mkv", [0, 0, 0])
    original = workflows._Frames.next
    def replacement(self):
        item = original(self)
        if item is not None:
            return item[0], item[1], arrays[item[0]]
        return None
    monkeypatch.setattr(workflows._Frames, "next", replacement)
    result = run_workflow(source, output_path=tmp_path / "result.bundle",
                          options=WorkflowOptions(workflow="first_change", threshold=0, min_changed_ratio=0.5))
    assert result.report["first_change"]["frame"] == 2
    assert result.report["difference_artifact_pair"]["frame"] == 2
    assert np.max(tensor(result, "workflow_difference").data.materialize()) == 1
