"""缓存引用保护覆盖数组句柄、运行中生成和独立进程。"""
import json
import multiprocessing
import os
import sys

import numpy as np
import pytest

from pixelprobe.engine.cache import CacheKeyInput, LocalArrayCache
from pixelprobe.engine.cache_lifecycle import CacheInUseError
from conftest import run_json


def _key():
    return CacheKeyInput(input_content_sha256="a" * 64, operator_name="sample.xt",
                         operator_version="1", canonical_config={}, input_tensor_descriptors=(),
                         dtype="uint8", precision="decoded", execution_semantics_version="1",
                         artifact_role="data")


def test_open_handle_protects_cache_even_from_another_instance(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    handle = cache.put(_key(), np.arange(6, dtype=np.uint8))
    other = LocalArrayCache(cache.root)
    try:
        with pytest.raises(CacheInUseError, match="CACHE_IN_USE"):
            other.clear()
        np.testing.assert_array_equal(handle.materialize(), np.arange(6, dtype=np.uint8))
    finally:
        handle.close()
    assert other.clear() == 1


def test_read_handle_keeps_reference_after_writer_closed(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    cache.put(_key(), np.zeros((2, 3), dtype=np.uint8)).close()
    hit = cache.get(_key()).handle
    try:
        with pytest.raises(CacheInUseError):
            cache.clear()
    finally:
        hit.close()
    assert cache.clear() == 1


def test_execution_reference_protects_partials_and_releases_on_error(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    with pytest.raises(RuntimeError):
        with cache.reference():
            with pytest.raises(CacheInUseError):
                cache.clear()
            raise RuntimeError("simulated execution failure")
    assert cache.clear() == 0


def test_unknown_files_and_unmarked_historical_partial_are_preserved(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    unknown = cache.entries / "personal"
    unknown.mkdir()
    (unknown / "keep.txt").write_text("keep", encoding="utf-8")
    historical = cache.partials / ("b" * 64)
    historical.mkdir()
    (historical / "data.npy").write_bytes(b"unknown")
    cache.put(_key(), np.zeros(3, dtype=np.uint8)).close()
    result = cache.maintain(dry_run=False)
    assert result["removed_entries"] == 1 and result["skipped"] == 2
    assert (unknown / "keep.txt").exists() and historical.exists()


def test_expiry_preview_then_apply_handles_owned_orphan_partial(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    orphan = cache.partials / ("b" * 64)
    orphan.mkdir()
    (orphan / ".pixelprobe-owned.json").write_text(json.dumps({
        "owner": "pixelprobe", "kind": "partial", "request_sha256": orphan.name,
    }), encoding="utf-8")
    (orphan / "data.npy").write_bytes(b"partial")
    assert cache.maintain(max_age_seconds=86400)["eligible_entries"] == 0
    preview = cache.maintain()
    assert preview["eligible_entries"] == 1 and preview["bytes"] > 0
    assert orphan.exists()
    assert cache.maintain(dry_run=False)["removed_entries"] == 1
    assert not orphan.exists()


def test_unknown_reference_file_is_never_removed(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    with cache.reference():
        pass
    marker = cache.root / ".refs" / "user.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(CacheInUseError):
        cache.clear()
    assert marker.read_text("utf-8") == "preserve"


def _reference_process(root, ready, release):
    with LocalArrayCache(root).reference():
        ready.set()
        release.wait(10)


def test_live_process_prevents_clear_and_process_exit_releases(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    cache.put(_key(), np.zeros(2, dtype=np.uint8)).close()
    context = multiprocessing.get_context("spawn")
    process_type = context.Process
    if sys.platform == "win32":
        from pixelprobe_mcp.windows_spawn import HiddenSpawnProcess
        process_type = HiddenSpawnProcess
    ready, release = context.Event(), context.Event()
    process = process_type(target=_reference_process, args=(cache.root, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(CacheInUseError):
            cache.clear()
        release.set()
        process.join(5)
        assert process.exitcode == 0
        assert cache.clear() == 1
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()
        process.close()


def test_cli_prune_defaults_to_preview(tmp_path):
    cache = LocalArrayCache(tmp_path / "cache")
    cache.put(_key(), np.zeros(2, dtype=np.uint8)).close()
    assert run_json("cache", "inspect", "--cache-dir", cache.root, "--json")["data"]["entries"] == 1
    preview = run_json("cache", "prune", "--cache-dir", cache.root,
                       "--older-than-days", "0", "--json")["data"]
    assert preview["dry_run"] and preview["eligible_entries"] == 1
    assert (cache.entries / _key().key()).exists()
    result = run_json("cache", "prune", "--cache-dir", cache.root,
                      "--older-than-days", "0", "--apply", "--json")["data"]
    assert result["removed_entries"] == 1
