"""发行附件的实际内容与不覆盖既有目录的契约。"""
import base64
import csv
import hashlib
import io
import json
import zipfile

import pytest
import build_release_inventory as inventory


def test_release_inventory_hashes_and_runtime_scope(tmp_path):
    binary = tmp_path / "pixelprobe.exe"
    binary.write_bytes(b"independent artifact bytes")
    output = tmp_path / "release"
    result = inventory.collect(output, binary, include_bundler=False)
    assert result["license_files_collected"]
    assert result["source_provenance_collected"]
    assert result["redistribution_review_complete"] is False
    runtime = {p["name"] for p in result["packages"] if p["role"] == "runtime"}
    assert {"pixelprobe", "av", "mcp", "c2pa-python", "zarr", "opencv-python-headless"} <= runtime
    assert not {"pytest", "build", "pyinstaller"} & runtime
    c2pa = next(p for p in result["packages"] if p["name"] == "c2pa-python")
    assert {p["origin"].rsplit("/", 1)[-1] for p in c2pa["license_files"]} >= {"LICENSE-MIT", "LICENSE-APACHE"}
    for line in (output / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        expected, name = line.split("  ", 1)
        target = binary if name == binary.name else output / name
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected
    saved = json.loads((output / "dependencies.json").read_text(encoding="utf-8"))
    assert saved["executable"]["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert (output / "licenses" / "CPython-LICENSE.txt").stat().st_size > 100
    assert (output / "LICENSE").is_file()
    sources = json.loads((output / "native-sources.json").read_text(encoding="utf-8"))
    assert sources["reviewed_python_packages"] == {
        "av": "18.1.0",
        "c2pa-python": "0.37.10",
        "opencv-python-headless": "5.0.0.93",
    }
    assert set(sources["python_wheels"]) == {
        "windows-x86_64", "linux-x86_64", "macos-arm64",
    }
    assert result["native_source_manifest"]["sha256"] == hashlib.sha256(
        (output / "native-sources.json").read_bytes()
    ).hexdigest()
    evidence = json.loads((output / "native-evidence.json").read_text(encoding="utf-8"))
    assert evidence["redistribution_review_complete"] is False
    if evidence["inspection_status"] == "binary_exports_collected":
        assert len(evidence["ffmpeg"]) == 2
        assert all(item["sha256"] and item["license"] and item["configuration"]
                   for item in evidence["ffmpeg"])


def test_release_inventory_refuses_existing_directory(tmp_path):
    target = tmp_path / "release"
    target.mkdir()
    marker = target / "user-file"
    marker.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        inventory.collect(target, tmp_path / "unused.exe")
    assert marker.read_bytes() == b"keep"
    assert list(target.iterdir()) == [marker]


def test_release_inventory_blocks_unreviewed_publication():
    with pytest.raises(RuntimeError, match="再分发审查未完成"):
        inventory.require_redistribution_review({
            "native_wheel_verification": {"verified": True},
            "redistribution_review_complete": False,
        })


def test_release_inventory_requires_native_wheel_verification():
    with pytest.raises(RuntimeError, match="未核验关键原生 wheel"):
        inventory.require_redistribution_review({
            "native_wheel_verification": {"verified": False},
            "redistribution_review_complete": True,
        })


def test_native_wheel_verification_compares_installed_runtime(tmp_path, monkeypatch):
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    site = tmp_path / "site"
    runtime_name = "demo/__init__.py"
    build_name = "demo/native.lib"
    runtime = b"verified runtime payload"
    build_input = b"wheel build input"
    for name, content in ((runtime_name, runtime), (build_name, b"changed after install")):
        target = site / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def record_hash(content):
        return base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()

    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\n")
    writer.writerow((runtime_name, f"sha256={record_hash(runtime)}", len(runtime)))
    writer.writerow((build_name, f"sha256={record_hash(build_input)}", len(build_input)))
    writer.writerow(("demo-1.0.dist-info/RECORD", "", ""))
    wheel = wheel_dir / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(runtime_name, runtime)
        archive.writestr(build_name, build_input)
        archive.writestr("demo-1.0.dist-info/RECORD", rows.getvalue())

    class Distribution:
        @staticmethod
        def locate_file(relative):
            return site / relative

    monkeypatch.setattr(inventory, "release_target", lambda: "windows-x86_64")
    monkeypatch.setattr(inventory.metadata, "distribution", lambda _name: Distribution())
    manifest = {"python_wheels": {"windows-x86_64": [{
        "package": "demo", "filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }]}}
    result = inventory.verify_native_wheels(wheel_dir, manifest)
    assert result["verified"] is True
    assert result["packages"][0]["installed_files_verified"] == 1
    assert result["packages"][0]["excluded_build_files"] == [build_name]

    (site / runtime_name).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="已安装文件与官方 wheel 不一致"):
        inventory.verify_native_wheels(wheel_dir, manifest)
