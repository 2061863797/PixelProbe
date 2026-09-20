"""使用当前 Python 环境构建仅供本地使用的 PixelProbe 单文件程序。"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DISTRIBUTIONS = (
    "pixelprobe", "pyinstaller", "av", "mcp", "opencv-python-headless",
    "zarr", "c2pa-python", "psutil",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def installed_versions() -> dict[str, str]:
    missing = []
    versions = {}
    for name in REQUIRED_DISTRIBUTIONS:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    if missing:
        raise RuntimeError(
            "缺少本地打包依赖：" + ", ".join(missing)
            + "。请先执行 python -m pip install -e \".[dev,mcp,flow,storage,evidence]\" pyinstaller"
        )
    return versions


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def build(output: Path, *, skip_distribution_tests: bool = False) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{output}")
    versions = installed_versions()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        dist = temporary / "dist"
        work = temporary / "work"
        run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(dist), "--workpath", str(work),
            str(ROOT / "packaging" / "pixelprobe.spec"),
        ])
        executable = dist / ("pixelprobe.exe" if os.name == "nt" else "pixelprobe")
        if not executable.is_file():
            raise RuntimeError(f"PyInstaller 未生成预期文件：{executable}")

        run([str(executable), "--version"])
        run([str(executable), "--help"])
        run([str(executable), "doctor", "--json"])
        if not skip_distribution_tests:
            environment = os.environ.copy()
            environment["PIXELPROBE_TEST_EXECUTABLE"] = str(executable)
            run([
                sys.executable, "-m", "pytest",
                "tests/test_mcp_server.py", "tests/test_distribution.py",
                "-k", "stdio_transport_handshake or distribution",
                "-p", "no:cacheprovider",
            ], env=environment)

        delivery = temporary / "delivery"
        delivery.mkdir()
        target_executable = delivery / executable.name
        shutil.copy2(executable, target_executable)
        for name in ("README.md", "README_EN.md", "LICENSE"):
            shutil.copy2(ROOT / name, delivery / name)
        (delivery / "LOCAL-BUILD.txt").write_text(
            "这是使用当前机器和当前 Python 环境生成的本地产物，不是 PixelProbe 官方发行物。\n"
            "它可能包含 PyAV、FFmpeg、OpenCV、C2PA 等第三方原生库。\n"
            "在完成对应许可与源码交付审查前，请勿公开再分发该目录或可执行文件。\n",
            encoding="utf-8",
        )
        (delivery / "build-info.json").write_text(json.dumps({
            "scope": "local_private_build",
            "redistribution_review_complete": False,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": versions,
            "executable": {
                "name": target_executable.name,
                "sha256": sha256(target_executable),
                "size_bytes": target_executable.stat().st_size,
            },
            "distribution_tests_run": not skip_distribution_tests,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(delivery, output)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="必须尚不存在的新目录")
    parser.add_argument(
        "--skip-distribution-tests", action="store_true",
        help="只执行启动/帮助/doctor 冒烟；默认还运行真实 CLI/MCP 发行验收",
    )
    args = parser.parse_args()
    try:
        result = build(args.output, skip_distribution_tests=args.skip_distribution_tests)
    except (FileExistsError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"错误：{exc}\n")
    print(json.dumps({"output": str(result), "scope": "local_private_build"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
