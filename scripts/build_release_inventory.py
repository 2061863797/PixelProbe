"""收集实际运行时依赖、上游许可副本、原生文件及发行程序校验和。"""
import argparse
import base64
import csv
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import sys
import sysconfig
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOURCES = ROOT / "packaging" / "native-sources.json"


def native_source_manifest():
    """读取经过版本控制的原生来源基线，并拒绝未审查的版本漂移。"""
    manifest = json.loads(NATIVE_SOURCES.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("不支持的原生来源清单版本")
    if manifest.get("source_provenance_collected") is not True:
        raise RuntimeError("原生依赖来源尚未收集完成")
    unresolved = manifest.get("unresolved_checks", [])
    if bool(manifest.get("redistribution_review_complete")) == bool(unresolved):
        raise RuntimeError("再分发审查状态与 unresolved_checks 不一致")
    required_targets = {"windows-x86_64", "linux-x86_64", "macos-arm64"}
    if set(manifest.get("python_wheels", {})) != required_targets:
        raise RuntimeError("原生来源清单未覆盖三个独立版目标平台")
    for target, wheels in manifest["python_wheels"].items():
        packages = {item["package"] for item in wheels}
        if packages != set(manifest["reviewed_python_packages"]):
            raise RuntimeError(f"{target} 的原生 wheel 清单不完整")
        for item in wheels:
            digest = item.get("sha256", "")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise RuntimeError(f"{target} 存在无效的 wheel SHA256")
    for name, expected in manifest["reviewed_python_packages"].items():
        actual = metadata.version(name)
        if actual != expected:
            raise RuntimeError(f"原生依赖版本漂移：{name} 应为 {expected}，实际为 {actual}")
    return manifest


def native_evidence(source_manifest):
    """保存真实 DLL 导出与构建信息，不据此推断完整再分发许可。"""
    result = {
        "source_provenance_collected": source_manifest["source_provenance_collected"],
        "redistribution_review_complete": source_manifest["redistribution_review_complete"],
        "reviewed_python_packages": source_manifest["reviewed_python_packages"],
        "pending_checks": source_manifest["unresolved_checks"],
        "ffmpeg": [],
    }
    if sys.platform != "win32":
        result["inspection_status"] = "not_performed_on_this_platform"
        return result
    import ctypes
    import av  # 先加载 wheel 自己的 DLL 搜索路径与依赖。
    import cv2
    package = metadata.distribution("av")
    for entry in package.files or []:
        if entry.name.startswith(("avcodec-", "avutil-")) and entry.suffix.lower() == ".dll":
            path = Path(package.locate_file(entry)).resolve()
            library = ctypes.CDLL(str(path))
            prefix = "avcodec" if entry.name.startswith("avcodec-") else "avutil"
            record = {"file": entry.name, "sha256": sha256(path)}
            for field in ("license", "configuration"):
                function = getattr(library, f"{prefix}_{field}")
                function.restype, function.argtypes = ctypes.c_char_p, []
                record[field] = function().decode("utf-8")
            result["ffmpeg"].append(record)
    if len(result["ffmpeg"]) != 2:
        raise RuntimeError("无法定位实际 avcodec/avutil DLL，不能生成完整原生检查证据")
    result.update(
        inspection_status="binary_exports_collected",
        pyav_version=av.__version__, ffmpeg_library_versions=av.library_versions,
        opencv_version=cv2.__version__, opencv_build_information=cv2.getBuildInformation(),
    )
    return result


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def release_target():
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    if sys.platform.startswith("linux") and machine in {"amd64", "x86_64"}:
        return "linux-x86_64"
    if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
        return "macos-arm64"
    raise RuntimeError(f"原生 wheel 核验不支持当前目标：{sys.platform}/{machine}")


def verify_native_wheels(wheel_dir, source_manifest):
    """核对官方 wheel 指纹，并以 RECORD 复验已安装的运行时文件。"""
    if wheel_dir is None:
        return {"verified": False, "reason": "wheel_dir_not_provided"}
    wheel_dir = Path(wheel_dir).resolve(strict=True)
    target = release_target()
    packages = []
    for expected in source_manifest["python_wheels"][target]:
        wheel = wheel_dir / expected["filename"]
        if not wheel.is_file():
            raise FileNotFoundError(f"缺少待核验 wheel：{wheel}")
        digest = sha256(wheel)
        if digest != expected["sha256"]:
            raise RuntimeError(f"wheel SHA256 不匹配：{wheel.name}")
        distribution = metadata.distribution(expected["package"])
        checked = 0
        excluded = []
        with zipfile.ZipFile(wheel) as archive:
            record_name = next(
                name for name in archive.namelist() if name.endswith(".dist-info/RECORD")
            )
            rows = csv.reader(archive.read(record_name).decode("utf-8").splitlines())
            for relative, digest_spec, _size in rows:
                if not digest_spec:
                    continue
                # 编译/链接输入不进入冻结程序；运行时文件必须逐项完全相同。
                if Path(relative).suffix.lower() in {".a", ".lib"}:
                    excluded.append(relative)
                    continue
                algorithm, encoded = digest_spec.split("=", 1)
                if algorithm != "sha256":
                    raise RuntimeError(f"wheel RECORD 使用未知哈希算法：{algorithm}")
                installed = Path(distribution.locate_file(relative))
                if not installed.is_file():
                    raise RuntimeError(f"已安装 wheel 缺少文件：{relative}")
                actual = base64.urlsafe_b64encode(
                    hashlib.sha256(installed.read_bytes()).digest()
                ).rstrip(b"=").decode("ascii")
                if actual != encoded:
                    raise RuntimeError(f"已安装文件与官方 wheel 不一致：{relative}")
                checked += 1
        packages.append({
            "package": expected["package"], "filename": wheel.name,
            "sha256": digest, "installed_files_verified": checked,
            "excluded_build_files": excluded,
        })
    return {"verified": True, "target": target, "packages": packages}


def runtime_dependencies():
    pending = [("pixelprobe", {"mcp", "flow", "storage", "evidence"})]
    seen = {}
    while pending:
        name, extras = pending.pop()
        name = canonicalize_name(name)
        previous = seen.get(name)
        if previous is not None and extras <= previous:
            continue
        extras |= previous or set()
        seen[name] = extras
        for text in metadata.distribution(name).requires or []:
            requirement = Requirement(text)
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in extras | {""}
            ):
                installed = metadata.version(requirement.name)
                if requirement.specifier and installed not in requirement.specifier:
                    raise RuntimeError(f"依赖版本不匹配：{requirement.name} {installed}")
                pending.append((requirement.name, set(requirement.extras)))
    return sorted(seen)


def collect(output, executable, *, include_bundler=True, wheel_dir=None):
    source_manifest = native_source_manifest()
    wheel_verification = verify_native_wheels(wheel_dir, source_manifest)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # 不覆盖已有版本清单或许可。
    for filename in ("README.md", "README_EN.md", "LICENSE"):
        (output / filename).write_bytes((ROOT / filename).read_bytes())
    records, missing = [], []
    runtime = runtime_dependencies()
    for name in sorted(set(runtime) | ({"pyinstaller"} if include_bundler else set())):
        package = metadata.distribution(name)
        record = {
            "name": name, "version": package.version,
            "role": "runtime" if name in runtime else "bundler",
            "license": package.metadata.get("License-Expression") or package.metadata.get("License"),
            "project_urls": package.metadata.get_all("Project-URL") or [],
            "license_files": [], "native_files": [],
        }

        def copy_notice(source, origin):
            relative = Path("licenses") / name / f"{len(record['license_files']):03d}-{source.name}"
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            record["license_files"].append({
                "path": relative.as_posix(), "sha256": sha256(target), "origin": origin,
            })

        for entry in sorted(package.files or [], key=str):
            path = Path(package.locate_file(entry))
            if not path.is_file():
                continue
            if any(token in entry.name.lower() for token in ("license", "licence", "copying", "notice", "authors")):
                copy_notice(path, str(entry).replace("\\", "/"))
            if path.suffix.lower() in {".dll", ".pyd", ".dylib", ".so"} or ".so." in path.name:
                record["native_files"].append({
                    "path": str(entry).replace("\\", "/"), "sha256": sha256(path),
                    "size_bytes": path.stat().st_size,
                })
        supplement = ROOT / "packaging" / "licenses" / f"{name}-{package.version}"
        if supplement.is_dir():
            source = json.loads((supplement / "SOURCE.json").read_text(encoding="utf-8"))
            assert source["package"] == name and source["version"] == package.version
            for entry in source["files"]:
                path = (supplement / entry["file"]).resolve()
                if not path.is_relative_to(supplement.resolve()) or sha256(path) != entry["sha256"]:
                    raise ValueError("补充许可路径或指纹校验失败")
                copy_notice(path, entry["url"])
        if not record["license_files"]:
            missing.append(f"{name}=={package.version}")
        records.append(record)
    python_license = next((p for p in (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
    ) if p.is_file()), None)
    if python_license is None:
        missing.append("CPython LICENSE.txt")
    else:
        (output / "licenses" / "CPython-LICENSE.txt").write_bytes(python_license.read_bytes())
    native_sources_target = output / "native-sources.json"
    native_sources_target.write_bytes(NATIVE_SOURCES.read_bytes())
    review_complete = bool(source_manifest["redistribution_review_complete"])
    manifest = {
        "schema_version": 2, "license_files_collected": not missing,
        "source_provenance_collected": source_manifest["source_provenance_collected"],
        "redistribution_review_complete": review_complete,
        "python": platform.python_version(), "platform": platform.platform(),
        "runtime_package_count": len(runtime), "packages": records,
        "missing_licenses": missing,
        "executable": {"name": executable.name, "sha256": sha256(executable),
                       "size_bytes": executable.stat().st_size},
        "scope": "installed_runtime_dependency_closure",
        "bundler_included": include_bundler,
        "native_source_manifest": {
            "path": native_sources_target.name,
            "sha256": sha256(native_sources_target),
            "reviewed_python_packages": source_manifest["reviewed_python_packages"],
            "unresolved_check_count": len(source_manifest["unresolved_checks"]),
        },
        "native_wheel_verification": wheel_verification,
        "limitations": [
            "原生文件列表来自已安装 wheel，不等于 PyInstaller 实际嵌入文件清单。",
            "上游随包许可收集不证明嵌入原生库的全部传递依赖和再分发要求已经审核。",
        ],
    }
    evidence = native_evidence(source_manifest)
    (output / "native-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (output / "dependencies.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (output / "runtime-requirements.txt").write_text(
        "".join(f"{p['name']}=={p['version']}\n" for p in records if p["role"] == "runtime"), encoding="utf-8")
    (output / "THIRD-PARTY-NOTICES.txt").write_text(
        "许可原文位于 licenses/；版本、来源、SHA256 与原生文件见 dependencies.json。\n"
        "原生 wheel、上游构建提交、源码包和发布包 SHA256 见 native-sources.json。\n"
        "清单记录本次构建环境的运行时依赖及 PyInstaller。\n"
        "再分发审查状态："
        + ("已完成。\n" if review_complete else "未完成；正式标签发布门槛必须拒绝本候选。\n")
        + "原生库内嵌的其他组件须依据对应构建的上游通知审核；不以本清单替代该审核。\n",
        encoding="utf-8")
    checksums = [(sha256(p), p.relative_to(output).as_posix()) for p in sorted(output.rglob("*")) if p.is_file()]
    checksums.insert(0, (sha256(executable), executable.name))
    (output / "SHA256SUMS.txt").write_text("".join(f"{digest}  {name}\n" for digest,name in checksums), encoding="utf-8")
    if missing:
        raise RuntimeError("缺少许可文件：" + ", ".join(missing))
    return manifest


def require_redistribution_review(manifest):
    if not manifest["native_wheel_verification"]["verified"]:
        raise RuntimeError("正式发布未核验关键原生 wheel 及其已安装文件")
    if not manifest["redistribution_review_complete"]:
        raise RuntimeError(
            "原生依赖再分发审查未完成；详见 native-sources.json 的 unresolved_checks"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument(
        "--wheel-dir", type=Path,
        help="含 native-sources.json 所列当前平台 wheel 的目录；正式发布必须提供",
    )
    parser.add_argument(
        "--require-redistribution-review", action="store_true",
        help="正式发布时要求原生依赖再分发审查已完成，否则失败",
    )
    args = parser.parse_args()
    result = collect(
        args.output, args.executable.resolve(strict=True), wheel_dir=args.wheel_dir,
    )
    if args.require_redistribution_review:
        try:
            require_redistribution_review(result)
        except RuntimeError as exc:
            parser.exit(3, f"错误：{exc}\n")
    print(json.dumps({"license_files_collected": result["license_files_collected"],
                      "redistribution_review_complete": result["redistribution_review_complete"],
                      "runtime_package_count": result["runtime_package_count"]}))
