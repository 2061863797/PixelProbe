"""高级证据分析：可追溯结果、适用边界与 C2PA 状态。"""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from conftest import run_json
from pixelprobe.artifacts import BundleReader
from pixelprobe.evidence import _c2pa, analyze_evidence
from pixelprobe.domain.errors import MediaChangedDuringAnalysisError


def test_evidence_writes_verifiable_bundle_without_authenticity_verdict(
    tmp_path: Path,
) -> None:
    source = tmp_path / "重压缩样本.jpg"
    image = Image.new("RGB", (64, 48), (30, 60, 90))
    for x in range(32, 64):
        for y in range(48):
            image.putpixel((x, y), (220, 180, 40))
    image.save(source, quality=82)

    result = analyze_evidence(source, output_path=tmp_path / "evidence.bundle")

    assert result.report["automatic_authenticity_conclusion"] is None
    assert result.report["ela"]["status"] == "completed"
    assert isinstance(result.report["noise_consistency"]["candidate_regions"], list)
    assert result.report["edge_candidates"]["candidate_regions"]
    bundle = BundleReader().open(result.bundle.root, verify="full")
    assert any(record.kind == "data" for record in bundle.manifest.artifacts)
    assert sum(record.kind == "preview" for record in bundle.manifest.artifacts) == 3


def test_evidence_marks_ela_not_applicable_for_png(tmp_path: Path) -> None:
    source = tmp_path / "无损.png"
    Image.new("RGB", (24, 24), (1, 2, 3)).save(source)
    result = analyze_evidence(source, output_path=tmp_path / "png.bundle")
    assert result.report["ela"]["status"] == "not_applicable"
    assert result.report["c2pa"]["verification_state"] in {
        "not_checked", "no_credentials", "verification_failed", "trust_unknown",
    }


@pytest.mark.parametrize("reason", ["远程凭证未获取", "C2PA 原生依赖无法加载"])
def test_mcp_evidence_reports_actual_unavailable_reason(tmp_path, monkeypatch, reason):
    from pixelprobe_mcp.config import ServerConfig
    from pixelprobe_mcp.models import CapabilityInput
    from pixelprobe_mcp.service import PixelProbeService
    source = tmp_path / "input.png"
    Image.new("RGB", (16, 16)).save(source)
    monkeypatch.setattr("pixelprobe.evidence._c2pa", lambda _: {
        "status": "unavailable", "verification_state": "not_checked", "reason": reason,
    })
    service = PixelProbeService(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    result, _ = service.run_capability(CapabilityInput(capability="evidence", media_path=str(source)))
    assert any(reason in warning for warning in result.warnings)
    assert all("未安装" not in warning for warning in result.warnings)
    assert result.data["metadata"]["exif"] == {} and result.data["metadata"]["gps_present"] is False


def _photo_with_exif(path: Path) -> None:
    from PIL import ExifTags
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "TestCam"
    exif[ExifTags.Base.Model] = "Model X"
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[ExifTags.Base.DateTimeOriginal] = "2026:10:01 08:30:00"
    sub[ExifTags.Base.LensModel] = "50mm F1.8"
    sub[ExifTags.Base.MakerNote] = b"\x00" * 600
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (31.0, 14.0, 0.0)
    Image.new("RGB", (32, 32), (90, 90, 90)).save(path, exif=exif, quality=90)


def test_evidence_returns_exif_values_and_hides_gps_by_default(tmp_path):
    """回归：MCP 曾只返回 EXIF 字段名；现在 CLI 与 MCP 都返回值（含 Exif 子目录），GPS 需显式开启。"""
    from pixelprobe_mcp.config import ServerConfig
    from pixelprobe_mcp.models import CapabilityInput
    from pixelprobe_mcp.service import PixelProbeService
    source = tmp_path / "photo.jpg"
    _photo_with_exif(source)
    service = PixelProbeService(ServerConfig((tmp_path,), tmp_path / "artifacts"))
    result, _ = service.run_capability(CapabilityInput(capability="evidence", media_path=str(source)))
    metadata = result.data["metadata"]
    assert metadata["exif"]["Model"] == "Model X"
    assert metadata["exif"]["DateTimeOriginal"] == "2026:10:01 08:30:00"
    assert metadata["exif"]["LensModel"] == "50mm F1.8"
    assert "MakerNote" in metadata["exif_omitted_fields"] and "MakerNote" not in metadata["exif"]
    assert metadata["gps_present"] is True and "gps" not in metadata
    assert any("include_gps" in warning for warning in result.warnings)
    shown, _ = service.run_capability(CapabilityInput(
        capability="evidence", media_path=str(source), options={"include_gps": True},
    ))
    assert shown.data["metadata"]["gps"]["GPSLatitudeRef"] == "N"
    # 写入结果包的完整报告保留 GPS 与 MakerNote，不受返回值裁剪影响
    full = analyze_evidence(source, output_path=tmp_path / "full.bundle").report["metadata"]
    assert full["gps"]["GPSLatitudeRef"] == "N" and "MakerNote" in full["exif"]


def test_c2pa_adapter_separates_validation_failure_from_unknown_trust(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "asset.jpg"
    source.write_bytes(b"asset")

    class Context:
        @classmethod
        def from_dict(cls, value):
            return cls()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None

    class Reader:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def json(self):
            return json.dumps({
                "active_manifest": "claim",
                "manifests": {"claim": {"assertions": [], "ingredients": []}},
            })

    errors = SimpleNamespace(ManifestNotFound=type("ManifestNotFound", (Exception,), {}),
                             RemoteManifest=type("RemoteManifest", (Exception,), {}))
    monkeypatch.setitem(sys.modules, "c2pa", SimpleNamespace(Context=Context, Reader=Reader, C2paError=errors))
    assert _c2pa(source)["verification_state"] == "trust_unknown"


def test_evidence_cli_json_reports_bundle(tmp_path: Path) -> None:
    source = tmp_path / "cli.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(source)
    payload = run_json("evidence", str(source), "--json")
    assert payload["success"] is True
    assert Path(payload["data"]["bundle_path"]).is_dir()
    assert payload["data"]["automatic_authenticity_conclusion"] is None


def test_evidence_rejects_changed_source_before_bundle_commit(tmp_path, monkeypatch):
    source = tmp_path / "change.png"
    target = tmp_path / "result.bundle"
    Image.new("RGB", (16, 16)).save(source)

    def replace_source(path):
        Image.new("RGB", (16, 16), "red").save(path)
        return {"status": "unavailable", "verification_state": "not_checked"}

    monkeypatch.setattr("pixelprobe.evidence._c2pa", replace_source)
    with pytest.raises(MediaChangedDuringAnalysisError):
        analyze_evidence(source, output_path=target)
    assert not target.exists()


def test_evidence_pixel_limit_runs_before_image_load(tmp_path, monkeypatch):
    source = tmp_path / "large.png"
    Image.new("RGB", (32, 32)).save(source)
    from PIL.PngImagePlugin import PngImageFile

    def forbidden_load(self, *args, **kwargs):
        pytest.fail("超过像素预算时不应开始解码")

    monkeypatch.setattr(PngImageFile, "load", forbidden_load)
    monkeypatch.setattr("pixelprobe.evidence._c2pa", lambda _: {
        "status": "unavailable", "verification_state": "not_checked",
    })
    result = analyze_evidence(source, output_path=tmp_path / "limit.bundle", max_pixels=100)
    assert result.report["ela"]["status"] == "failed"


def test_evidence_cancellation_between_methods_does_not_commit(tmp_path, monkeypatch):
    from pixelprobe.engine.execution import CancellationToken
    from pixelprobe.engine.errors import ExecutionCancelledError
    import pixelprobe.evidence as evidence
    source = tmp_path / "cancel.jpg"
    Image.new("RGB", (16, 16)).save(source)
    target = tmp_path / "cancel.bundle"
    cancellation = CancellationToken()
    original = evidence._ela
    def cancel_after_ela(*args, **kwargs):
        result = original(*args, **kwargs)
        cancellation.cancel()
        return result
    monkeypatch.setattr(evidence, "_ela", cancel_after_ela)
    with pytest.raises(ExecutionCancelledError):
        analyze_evidence(source, output_path=target, cancellation=cancellation)
    assert not target.exists()


def test_evidence_writes_numeric_data_artifacts_with_precise_provenance_and_validity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.jpg"
    image = Image.new("RGB", (32, 32), (50, 100, 150))
    for x in range(16, 32):
        for y in range(32):
            image.putpixel((x, y), (200, 150, 50))
    image.save(source, quality=85)

    target = tmp_path / "evidence_full.bundle"
    result = analyze_evidence(source, output_path=target, block_size=16)

    bundle = BundleReader().open(result.bundle.root, verify="full")

    data_records = {item.role: item for item in bundle.manifest.artifacts if item.kind == "data"}
    preview_records = {item.role: item for item in bundle.manifest.artifacts if item.kind == "preview"}

    assert "tensor_evidence_report" in data_records
    assert "tensor_evidence_ela" in data_records
    assert "tensor_evidence_noise" in data_records
    assert "tensor_evidence_edge" in data_records

    assert "preview_evidence_ela" in preview_records
    assert "preview_evidence_noise" in preview_records
    assert "preview_evidence_edge" in preview_records

    ela_preview_schema = json.loads(
        (bundle.root / bundle.artifact(preview_records["preview_evidence_ela"].tensor_schema_artifact_id).storage.files[0].uri).read_text("utf-8")
    )
    noise_preview_schema = json.loads(
        (bundle.root / bundle.artifact(preview_records["preview_evidence_noise"].tensor_schema_artifact_id).storage.files[0].uri).read_text("utf-8")
    )
    edge_preview_schema = json.loads(
        (bundle.root / bundle.artifact(preview_records["preview_evidence_edge"].tensor_schema_artifact_id).storage.files[0].uri).read_text("utf-8")
    )

    assert ela_preview_schema["descriptor"]["attributes"]["source_tensor_id"] == "tensor_evidence_ela"
    assert noise_preview_schema["descriptor"]["attributes"]["source_tensor_id"] == "tensor_evidence_noise"
    assert edge_preview_schema["descriptor"]["attributes"]["source_tensor_id"] == "tensor_evidence_edge"

    assert ela_preview_schema["descriptor"]["attributes"]["dag_input_node_ids"] == ["evidence.data.ela"]
    assert noise_preview_schema["descriptor"]["attributes"]["dag_input_node_ids"] == ["evidence.data.noise"]
    assert edge_preview_schema["descriptor"]["attributes"]["dag_input_node_ids"] == ["evidence.data.edge"]

    report_tensor = bundle.open_tensor(data_records["tensor_evidence_report"].artifact_id)
    try:
        assert report_tensor.validity is not None
        validity = report_tensor.validity.materialize()
        assert np.array_equal(validity, [True, True, True])
        metrics = report_tensor.data.materialize()
        assert not np.isnan(metrics).any()
    finally:
        report_tensor.data.close()  # type: ignore[attr-defined]
        if report_tensor.validity is not None:
            report_tensor.validity.close()  # type: ignore[attr-defined]


def test_evidence_png_marks_ela_invalid_in_report_validity_mask(
    tmp_path: Path,
) -> None:
    source = tmp_path / "lossless.png"
    Image.new("RGB", (24, 24), (80, 90, 100)).save(source)

    target = tmp_path / "png_evidence.bundle"
    result = analyze_evidence(source, output_path=target, block_size=12)

    assert result.report["ela"]["status"] == "not_applicable"

    bundle = BundleReader().open(result.bundle.root, verify="full")
    data_records = {item.role: item for item in bundle.manifest.artifacts if item.kind == "data"}
    preview_records = {item.role: item for item in bundle.manifest.artifacts if item.kind == "preview"}

    assert "tensor_evidence_ela" not in data_records
    assert "preview_evidence_ela" not in preview_records
    assert "tensor_evidence_noise" in data_records
    assert "tensor_evidence_edge" in data_records

    report_tensor = bundle.open_tensor(data_records["tensor_evidence_report"].artifact_id)
    try:
        assert report_tensor.validity is not None
        validity = report_tensor.validity.materialize()
        assert validity[0] == False
        assert validity[1] == True
        assert validity[2] == True

        metrics = report_tensor.data.materialize()
        assert np.isnan(metrics[0])
        assert not np.isnan(metrics[1])
        assert not np.isnan(metrics[2])
    finally:
        report_tensor.data.close()  # type: ignore[attr-defined]
        if report_tensor.validity is not None:
            report_tensor.validity.close()  # type: ignore[attr-defined]


def test_evidence_numeric_definitions_on_known_small_array(
    tmp_path: Path,
) -> None:
    arr = np.zeros((8, 8, 3), dtype=np.uint8)
    arr[:, 4:, :] = 100

    source = tmp_path / "step_edge.png"
    Image.fromarray(arr).save(source)

    target = tmp_path / "step.bundle"
    result = analyze_evidence(source, output_path=target, block_size=8)
    bundle = BundleReader().open(result.bundle.root, verify="full")

    expected_edge_score = 100.0 / (2.0 * np.sqrt(2.0))
    expected_mean_gradient = (8.0 * 100.0) / 64.0

    assert abs(result.report["edge_candidates"]["mean_gradient"] - expected_mean_gradient) < 1e-4

    edge_record = next(item for item in bundle.manifest.artifacts if item.role == "tensor_evidence_edge")
    edge_tensor = bundle.open_tensor(edge_record.artifact_id)
    try:
        edge_data = edge_tensor.data.materialize()
        assert edge_data.shape == (8, 8)
        assert np.allclose(edge_data, expected_edge_score, atol=1e-4)
    finally:
        edge_tensor.data.close()  # type: ignore[attr-defined]

    pure_source = tmp_path / "pure.png"
    Image.new("RGB", (8, 8), (128, 128, 128)).save(pure_source)
    pure_result = analyze_evidence(pure_source, output_path=tmp_path / "pure.bundle", block_size=8)
    pure_bundle = BundleReader().open(pure_result.bundle.root, verify="full")

    assert pure_result.report["noise_consistency"]["mean_residual"] == 0.0
    noise_record = next(item for item in pure_bundle.manifest.artifacts if item.role == "tensor_evidence_noise")
    noise_tensor = pure_bundle.open_tensor(noise_record.artifact_id)
    try:
        noise_data = noise_tensor.data.materialize()
        assert noise_data.shape == (8, 8)
        assert np.allclose(noise_data, 0.0)
    finally:
        noise_tensor.data.close()  # type: ignore[attr-defined]

    # 3. ELA 数值定义验证：JPEG 来源下每通道差值及均值完全吻合
    jpeg_source = tmp_path / "ela_sample.jpg"
    Image.fromarray(arr).save(jpeg_source, quality=80)
    ela_result = analyze_evidence(jpeg_source, output_path=tmp_path / "ela.bundle", block_size=8, jpeg_quality=80)
    ela_bundle = BundleReader().open(ela_result.bundle.root, verify="full")

    ela_record = next(item for item in ela_bundle.manifest.artifacts if item.role == "tensor_evidence_ela")
    ela_tensor = ela_bundle.open_tensor(ela_record.artifact_id)
    try:
        ela_data = ela_tensor.data.materialize()
        assert ela_data.shape == (8, 8, 3)
        assert ela_data.dtype == np.uint8
        expected_mean = float(np.round(ela_data.mean(), 6))
        assert ela_result.report["ela"]["mean_absolute_difference"] == expected_mean
        assert ela_result.report["ela"]["max_absolute_difference"] == int(ela_data.max())
    finally:
        ela_tensor.data.close()  # type: ignore[attr-defined]
