"""V1.0 Bundle 校验与缓存清理 CLI 验收测试。"""

from __future__ import annotations

from pathlib import Path

import numpy as np

import pixelprobe
from conftest import RED_Y, run_json, run_json_error
from pixelprobe.domain.geometry import PathGeometry
from pixelprobe.domain.media import MediaSource
from pixelprobe.domain.time import TemporalSelection
from pixelprobe.engine import (
    CacheKeyInput,
    LocalArrayCache,
    OutputRequest,
    RepresentationRequest,
)


def _request(path: Path) -> RepresentationRequest:
    return RepresentationRequest(
        source=MediaSource(source_id="source_main", kind="file", uri=str(path)),
        selection=TemporalSelection(mode="all"),
        representation="xt",
        geometry=PathGeometry(
            type="line", coordinate_space_id="storage_pixels",
            points=((0.0, float(RED_Y)), (31.0, float(RED_Y))),
        ),
        output=OutputRequest(format="bundle", include_preview=False),
    )


def test_validate_cli_checks_full_content(test_video: Path, tmp_path: Path) -> None:
    target = tmp_path / "validated.bundle"
    result = pixelprobe.generate(_request(test_video), output_path=target)
    data = run_json("validate", target, "--json")["data"]
    assert data["bundle_id"] == result.bundle.manifest.bundle_id
    assert data["content_integrity_verified"] is True

    record = next(
        item for item in result.bundle.manifest.artifacts if item.kind == "data"
    )
    path = target.joinpath(*record.storage.files[0].uri.split("/"))
    with path.open("ab") as handle:
        handle.write(b"tamper")
    exit_code, error = run_json_error("validate", target, "--json")
    assert exit_code == 2
    assert error["error"]["code"] == "ARTIFACT_CHECKSUM_MISMATCH"


def test_cache_clear_never_touches_bundle(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache = LocalArrayCache(cache_root)
    key = CacheKeyInput(
        input_content_sha256="a" * 64,
        operator_name="sample.xt",
        operator_version="1.0.0",
        canonical_config={},
        input_tensor_descriptors=(),
        dtype="uint8",
        precision="decoded",
        execution_semantics_version="0.1.0",
        artifact_role="data",
    )
    handle = cache.put(key, np.zeros((2, 3), dtype=np.uint8))
    handle.close()
    bundle = tmp_path / "formal.bundle"
    bundle.mkdir()
    marker = bundle / "manifest.json"
    marker.write_text("keep", encoding="utf-8")

    data = run_json(
        "cache", "clear", "--cache-dir", cache_root, "--json",
    )["data"]
    assert data["removed_entries"] == 1
    assert marker.read_text(encoding="utf-8") == "keep"


def test_generate_standalone_npy_is_exact_and_persistent(
    test_video: Path, tmp_path: Path,
) -> None:
    request = _request(test_video).model_copy(update={
        "output": OutputRequest(format="npy", include_preview=False),
    })
    target = tmp_path / "xt.npy"
    result = pixelprobe.generate(request, output_path=target)
    assert result.bundle is None
    assert target.is_file()
    tensor = result.request_tensors[0][0]
    assert tensor.data.storage_kind.value == "npy"
    assert np.array_equal(tensor.data.materialize(), np.load(target, allow_pickle=False))
    tensor.data.close()  # type: ignore[attr-defined]
