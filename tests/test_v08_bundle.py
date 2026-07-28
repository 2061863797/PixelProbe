"""V0.8 Bundle、Artifact 与请求 Schema 验收测试。"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from conftest import RED_Y
from pixelprobe import core
from pixelprobe.artifacts import BundleManifest, BundleReader, BundleWriter, save_zarr
from pixelprobe.artifacts.errors import (
    ArtifactChecksumMismatchError,
    ArtifactSchemaMismatchError,
    BundleManifestInvalidError,
    BundlePathUnsafeError,
    BundleTargetExistsError,
    BundleWriteError,
    ProvenanceGraphInvalidError,
    ZarrDependencyMissingError,
)
import pixelprobe.artifacts.bundle as bundle_module
from pixelprobe.domain.geometry import PathGeometry
from pixelprobe.domain.media import MediaSource
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.domain.tensor import MemoryArrayHandle
from pixelprobe.engine import RepresentationRequest


class _NoMaterializeHandle:
    def __init__(self, source) -> None:
        self.source = source

    @property
    def shape(self):
        return self.source.shape

    @property
    def dtype(self):
        return self.source.dtype

    @property
    def storage_kind(self):
        return self.source.storage_kind

    @property
    def chunk_shape(self):
        return self.source.chunk_shape

    def read(self, selection):
        return self.source.read(selection)

    def materialize(self, *, max_bytes=None):
        raise AssertionError("Bundle 数值写入不得整体 materialize")


def test_representation_request_is_typed_and_geometry_aware() -> None:
    request = RepresentationRequest(
        source=MediaSource(source_id="source_main", kind="file", uri="input.mkv"),
        selection=TemporalSelection(mode="all"),
        representation="path_t",
        geometry=PathGeometry(
            type="line",
            coordinate_space_id="storage_pixels",
            points=((0.0, 4.0), (31.0, 4.0)),
        ),
    )
    assert request.model_dump(mode="json")["output"]["format"] == "bundle"
    assert "geometry" in RepresentationRequest.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        RepresentationRequest(
            source=request.source,
            selection=request.selection,
            representation="roi_t",
            geometry=request.geometry,
        )


def test_bundle_round_trip_preserves_tensor_exactly(
    test_video: Path, tmp_path: Path,
) -> None:
    source = core.create_xt_slice(test_video, RED_Y).tensor
    target = tmp_path / "result.bundle"
    bundle = BundleWriter().write(target, (source,))
    assert bundle.manifest.schema_version == "0.1.0"
    assert bundle.manifest.complete is True
    assert not (target / "cache").exists()
    data_record = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    restored = bundle.open_tensor(data_record.artifact_id)
    try:
        assert restored.tensor_id == source.tensor_id
        assert restored.data.dtype == source.data.dtype
        assert [axis.name for axis in restored.axes] == ["time", "x", "channel"]
        assert np.array_equal(restored.data.materialize(), source.data.materialize())
        time_ref = restored.axes[0].coordinates_ref
        assert time_ref is not None and not time_ref.uri.startswith("memory://")
        index_record = bundle.artifact(time_ref.artifact_id)
        assert index_record.kind == "index"
        index_path = target.joinpath(*time_ref.uri.split("/"))
        assert np.array_equal(
            np.load(index_path),
            np.asarray(source.attributes["timeline_timestamps_seconds"]),
        )
    finally:
        restored.data.close()  # type: ignore[attr-defined]


def test_bundle_round_trip_preserves_broadcastable_validity_mask(
    test_video: Path, tmp_path: Path,
) -> None:
    source = core.create_xt_slice(test_video, RED_Y).tensor
    validity_values = np.zeros((source.data.shape[0], 1, 1), dtype=bool)
    validity_values[::2] = True
    tensor = replace(
        source, validity=MemoryArrayHandle(validity_values),
    )
    bundle = BundleWriter().write(tmp_path / "validity.bundle", (tensor,))
    data_record = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    schema_record = bundle.artifact(data_record.tensor_schema_artifact_id)
    schema = json.loads(
        (bundle.root / schema_record.storage.files[0].uri).read_text("utf-8")
    )
    validity_ref = schema["descriptor"]["validity_ref"]
    validity_record = bundle.artifact(validity_ref["artifact_id"])
    assert validity_record.kind == "index"
    assert validity_record.role == "validity_mask"
    assert validity_record.storage.shape == validity_values.shape
    assert np.dtype(validity_record.storage.dtype) == np.dtype(bool)
    assert validity_record.attributes["source_data_artifact_id"] == data_record.artifact_id

    restored = bundle.open_tensor(data_record.artifact_id)
    try:
        assert restored.validity is not None
        assert restored.validity.shape == validity_values.shape
        assert np.dtype(restored.validity.dtype) == np.dtype(bool)
        assert np.array_equal(restored.validity.materialize(), validity_values)
    finally:
        restored.data.close()  # type: ignore[attr-defined]
        if restored.validity is not None:
            restored.validity.close()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("field", "value"),
    (("dtype", "<u1"), ("shape", [1, 1, 1])),
)
def test_bundle_reader_rejects_tampered_validity_schema(
    test_video: Path, tmp_path: Path, field: str, value: object,
) -> None:
    source = core.create_xt_slice(test_video, RED_Y).tensor
    tensor = replace(
        source,
        validity=MemoryArrayHandle(np.ones((source.data.shape[0], 1, 1), dtype=bool)),
    )
    target = tmp_path / f"bad-validity-{field}.bundle"
    bundle = BundleWriter().write(target, (tensor,))
    data_record = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    schema_record = bundle.artifact(data_record.tensor_schema_artifact_id)
    schema = json.loads(
        (target / schema_record.storage.files[0].uri).read_text("utf-8")
    )
    validity_id = schema["descriptor"]["validity_ref"]["artifact_id"]
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    validity_record = next(
        item for item in manifest["artifacts"] if item["artifact_id"] == validity_id
    )
    validity_record["storage"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactSchemaMismatchError):
        BundleReader().open(target, verify="metadata")


def test_bundle_npy_writer_streams_array_handle_without_materialize(
    test_video: Path, tmp_path: Path,
) -> None:
    source = core.create_xt_slice(test_video, RED_Y).tensor
    streamed = replace(source, data=_NoMaterializeHandle(source.data))
    bundle = BundleWriter().write(tmp_path / "streamed.bundle", (streamed,))
    data_record = next(
        item for item in bundle.manifest.artifacts if item.kind == "data"
    )
    restored = bundle.open_tensor(data_record.artifact_id)
    try:
        assert np.array_equal(
            restored.data.materialize(), source.data.materialize(),
        )
    finally:
        restored.data.close()  # type: ignore[attr-defined]


def test_bundle_persists_numeric_npy_as_explicit_little_endian(
    test_video: Path, tmp_path: Path,
) -> None:
    source = core.create_xt_slice(test_video, RED_Y).tensor
    values = source.data.materialize().astype(">u2")
    big_endian = replace(source, data=MemoryArrayHandle(values))
    bundle = BundleWriter().write(tmp_path / "endian.bundle", (big_endian,))
    record = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    assert record.storage.dtype == "<u2"
    restored = bundle.open_tensor(record.artifact_id)
    try:
        assert np.dtype(restored.data.dtype).byteorder in {"<", "="}
        assert np.array_equal(restored.data.materialize(), values)
    finally:
        restored.data.close()  # type: ignore[attr-defined]


def test_bundle_separates_data_and_preview(
    test_video: Path, tmp_path: Path,
) -> None:
    result = core.temporal_reduce(test_video, op="mean")
    target = tmp_path / "preview.bundle"
    bundle = BundleWriter().write(
        target, (result.data_tensor, result.preview_tensor)
    )
    data = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    preview = next(
        item for item in bundle.manifest.artifacts if item.kind == "preview"
    )
    assert data.storage.format == "npy"
    assert data.storage.files[0].uri.startswith("artifacts/")
    assert preview.storage.format == "png"
    assert preview.storage.files[0].uri.startswith("previews/")
    preview_path = target.joinpath(*preview.storage.files[0].uri.split("/"))
    with Image.open(preview_path) as image:
        restored = np.asarray(image)
    assert np.array_equal(restored, result.preview_tensor.data.materialize())
    provenance_record = bundle.artifact(bundle.manifest.provenance_artifact_id)
    provenance_path = target.joinpath(
        *provenance_record.storage.files[0].uri.split("/")
    )
    graph = json.loads(provenance_path.read_text(encoding="utf-8"))
    preview_node = next(
        node for node in graph["nodes"]
        if preview.artifact_id in node["output_artifact_ids"]
    )
    assert data.artifact_id in preview_node["input_artifact_ids"]


def test_bundle_full_validation_detects_tampering(
    test_video: Path, tmp_path: Path,
) -> None:
    source = core.create_xt_slice(test_video, RED_Y).tensor
    target = tmp_path / "tampered.bundle"
    bundle = BundleWriter().write(target, (source,))
    data_record = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    data_path = target.joinpath(*data_record.storage.files[0].uri.split("/"))
    with data_path.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ArtifactChecksumMismatchError):
        BundleReader().open(target, verify="full")


def test_bundle_reader_rejects_path_traversal(
    test_video: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "unsafe.bundle"
    BundleWriter().write(target, (core.create_xt_slice(test_video, RED_Y).tensor,))
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = next(item for item in manifest["artifacts"] if item["kind"] == "data")
    data["storage"]["files"][0]["uri"] = "../../outside.npy"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundlePathUnsafeError):
        BundleReader().open(target, verify="metadata")


def test_bundle_writer_does_not_overwrite_by_default(
    test_video: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "existing.bundle"
    tensor = core.create_xt_slice(test_video, RED_Y).tensor
    BundleWriter().write(target, (tensor,))
    with pytest.raises(BundleTargetExistsError):
        BundleWriter().write(target, (tensor,))
    assert BundleManifest.model_json_schema()["properties"]["complete"]["const"] is True
    assert not list(tmp_path.glob(".existing.bundle.tmp-*"))


def test_bundle_overwrite_failure_restores_previous_complete_bundle(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "rollback.bundle"
    original = core.create_xt_slice(test_video, RED_Y).tensor
    replacement = core.create_xt_slice(test_video, RED_Y + 1).tensor
    BundleWriter().write(target, (original,))
    expected = original.data.materialize()
    real_replace = os.replace

    def fail_new_bundle(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == target and ".tmp-" in source_path.name:
            raise OSError("模拟原子替换失败")
        real_replace(source, destination)

    monkeypatch.setattr(bundle_module.os, "replace", fail_new_bundle)
    with pytest.raises(BundleWriteError):
        BundleWriter().write(target, (replacement,), overwrite=True)

    restored = BundleReader().open(target, verify="full")
    data_record = next(
        item for item in restored.manifest.artifacts if item.kind == "data"
    )
    restored_tensor = restored.open_tensor(data_record.artifact_id)
    assert np.array_equal(restored_tensor.data.materialize(), expected)
    assert not list(tmp_path.glob(".rollback.bundle.backup-*"))


def test_bundle_cleanup_failure_is_isolated_and_reported(
    test_video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = core.temporal_reduce(test_video, op="mean")
    invalid_preview = replace(
        result.preview_tensor,
        data=MemoryArrayHandle(
            result.preview_tensor.data.materialize().astype(np.float32)
        ),
    )
    real_rmtree = bundle_module.shutil.rmtree

    def fail_temporary(path, *args, **kwargs):
        if ".cleanup.bundle.tmp-" in Path(path).name:
            raise OSError("模拟清理失败")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(bundle_module.shutil, "rmtree", fail_temporary)
    with pytest.raises(BundleWriteError, match="已隔离"):
        BundleWriter().write(tmp_path / "cleanup.bundle", (invalid_preview,))

    assert len(list(tmp_path.glob(".abandoned-*"))) == 1
    assert not (tmp_path / "cleanup.bundle").exists()


def test_bundle_unknown_optional_field_is_notice_but_strict_rejects(
    test_video: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "future.bundle"
    BundleWriter().write(target, (core.create_xt_slice(test_video, RED_Y).tensor,))
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["future_optional"] = {"enabled": True}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    bundle = BundleReader().open(target, verify="metadata")
    assert any("future_optional" in notice for notice in bundle.notices)
    with pytest.raises(BundleManifestInvalidError):
        BundleReader().open(target, verify="metadata", strict=True)


def test_bundle_reader_rejects_excessive_json_nesting(
    test_video: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "deep-json.bundle"
    BundleWriter().write(target, (core.create_xt_slice(test_video, RED_Y).tensor,))
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    nested: object = True
    for _ in range(70):
        nested = {"next": nested}
    manifest["future_optional"] = nested
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundleManifestInvalidError, match="嵌套深度"):
        BundleReader().open(target, verify="metadata")


def test_bundle_reader_validates_tensor_data_ref_semantics(
    test_video: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "bad-schema.bundle"
    bundle = BundleWriter().write(
        target, (core.create_xt_slice(test_video, RED_Y).tensor,),
    )
    data = next(item for item in bundle.manifest.artifacts if item.kind == "data")
    schema = bundle.artifact(data.tensor_schema_artifact_id)
    schema_path = target.joinpath(*schema.storage.files[0].uri.split("/"))
    content = schema_path.read_text(encoding="utf-8")
    bad_id = ("b" if data.artifact_id[0] != "b" else "c") + data.artifact_id[1:]
    schema_path.write_bytes(content.replace(data.artifact_id, bad_id).encode("utf-8"))
    with pytest.raises(ArtifactSchemaMismatchError):
        BundleReader().open(target, verify="metadata")


def test_bundle_reader_rejects_provenance_missing_reference(
    test_video: Path, tmp_path: Path,
) -> None:
    target = tmp_path / "bad-provenance.bundle"
    bundle = BundleWriter().write(
        target, (core.create_xt_slice(test_video, RED_Y).tensor,),
    )
    record = bundle.artifact(bundle.manifest.provenance_artifact_id)
    path = target.joinpath(*record.storage.files[0].uri.split("/"))
    data_id = next(item.artifact_id for item in bundle.manifest.artifacts if item.kind == "data")
    bad_id = ("b" if data_id[0] != "b" else "c") + data_id[1:]
    path.write_bytes(
        path.read_text(encoding="utf-8").replace(data_id, bad_id).encode("utf-8")
    )
    with pytest.raises(ProvenanceGraphInvalidError):
        BundleReader().open(target, verify="metadata")


def test_zarr_v3_is_optional_but_never_silently_downgrades(
    tmp_path: Path,
) -> None:
    source = np.arange(24, dtype=np.float32).reshape(4, 6)
    path = tmp_path / "chunks.zarr"
    if importlib.util.find_spec("zarr") is None:
        with pytest.raises(ZarrDependencyMissingError):
            save_zarr(source, path, chunk_shape=(2, 3))
        assert not path.exists()
        return
    handle = save_zarr(source, path, chunk_shape=(2, 3))
    assert handle.storage_kind.value == "zarr"
    assert handle.chunk_shape == (2, 3)
    assert np.array_equal(
        handle.read((slice(1, 4), slice(2, 5))),
        source[1:4, 2:5],
    )
    assert (path / "zarr.json").is_file()
