"""本地单文件构建脚本的原子交付与范围标记。"""
import json
import os
from pathlib import Path

import build_local_standalone as builder
import pytest


def test_local_builder_marks_private_artifact_and_runs_distribution_tests(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(builder, "installed_versions", lambda: {
        name: "test" for name in builder.REQUIRED_DISTRIBUTIONS
    })

    def fake_run(command, *, env=None):
        calls.append((command, env))
        if "PyInstaller" in command:
            dist = Path(command[command.index("--distpath") + 1])
            dist.mkdir(parents=True)
            (dist / ("pixelprobe.exe" if os.name == "nt" else "pixelprobe")).write_bytes(
                b"local executable"
            )

    monkeypatch.setattr(builder, "run", fake_run)
    output = builder.build(tmp_path / "local-build")
    info = json.loads((output / "build-info.json").read_text(encoding="utf-8"))
    assert info["scope"] == "local_private_build"
    assert info["redistribution_review_complete"] is False
    assert info["distribution_tests_run"] is True
    assert "请勿公开再分发" in (output / "LOCAL-BUILD.txt").read_text(encoding="utf-8")
    assert any("pytest" in command for command, _env in calls)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["local-build"]


def test_local_builder_refuses_existing_output_before_dependency_check(tmp_path, monkeypatch):
    output = tmp_path / "existing"
    output.mkdir()
    called = False

    def versions():
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(builder, "installed_versions", versions)
    with pytest.raises(FileExistsError):
        builder.build(output)
    assert called is False
