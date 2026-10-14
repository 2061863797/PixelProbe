"""CLI 数值基线快照：重构前后逐项比对数值输出，证明计算结果没有被改动。

用法：
    python scripts/capture_golden.py --write   # 生成或刷新 tests/golden/cli_golden.json
    python scripts/capture_golden.py --check   # 与基线比对，不一致时退出码为 1

每个用例记录两类内容：
- CLI ``--json`` 输出中的数值字段（去掉路径、耗时等每次运行都会变的字段）；
- 输出的 PNG/CSV/NPY 文件内容摘要（PNG 按解码后的像素计算，避免编码器差异）。

用例参数随 CLI 写法变化而更新，但期望值不应改变；需要改变期望值时必须说明原因。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / "tests" / "golden" / "cli_golden.json"
sys.path.insert(0, str(ROOT / "scripts"))

from generate_test_image import generate_test_image  # noqa: E402
from generate_test_video import (  # noqa: E402
    BLINK_RECT,
    GREEN_POS,
    RED_Y,
    generate_blink_video,
    generate_motion_video,
    generate_noise_video,
    generate_test_video,
    generate_vfr_video,
)

FLOAT_TOLERANCE = 1e-6

# 每次运行都会变化、或只与本机环境有关的字段，比对时忽略
VOLATILE_KEYS = {
    "elapsed_seconds", "elapsed_ms", "duration_ms", "created_at", "generated_at",
    "bundle_id", "bundle_path", "path", "media", "media_path", "source_path",
    "output", "output_path", "csv_path", "json_path", "cache_root", "run_id",
    "job_id", "request_sha256", "input_sha256", "timestamp", "started_at",
    "finished_at", "warnings", "notices",
    # 测试视频由编码器生成，容器里带随机 UID，文件哈希每次不同；像素内容一致
    "source_sha256", "sha256", "media_sha256",
}


def _volatile(key: str) -> bool:
    return key in VOLATILE_KEYS or key.endswith(("_path", "_output_path", "_dir"))


def build_assets(root: Path) -> dict[str, Path]:
    return {
        "image": generate_test_image(root / "image.png"),
        "video": generate_test_video(root / "video.mkv"),
        "vfr": generate_vfr_video(root / "vfr.mkv"),
        "noise": generate_noise_video(root / "noise.mkv"),
        "blink": generate_blink_video(root / "blink.mkv"),
        "motion": generate_motion_video(root / "motion.mkv"),
    }


def build_cases() -> list[dict[str, Any]]:
    """用例：{id, args}。args 中 "{video}" 等占位符指向素材，"{out:name}" 指向输出文件。"""
    gx, gy = GREEN_POS
    blink_rect = ",".join(map(str, BLINK_RECT))
    return [
        {"id": "info_image", "args": ["info", "{image}"]},
        {"id": "info_video", "args": ["info", "{video}"]},
        {"id": "info_vfr", "args": ["info", "{vfr}"]},
        {"id": "frame_index", "args": ["frame", "{video}", "--frame", "7", "--output", "{out:frame7.png}"]},
        {"id": "frame_time", "args": ["frame", "{video}", "--time", "0.5", "--output", "{out:frame_t.png}"]},
        {"id": "frame_crop", "args": ["frame", "{video}", "--frame", "10", "--crop", "8,6,8,8", "--output", "{out:crop.png}"]},
        {"id": "frame_vfr_time", "args": ["frame", "{vfr}", "--time", "0.3", "--output", "{out:vfr.png}"]},
        {"id": "pixel_image", "args": ["pixel", "{image}", "--point", "0,0", "--point", "15,15", "--point", "3,5"]},
        {"id": "pixel_image_native", "args": ["pixel", "{image}", "--point", "0,0", "--sample", "native"]},
        {"id": "pixel_video", "args": ["pixel", "{video}", "--frame", "12", "--point", "12,8", "--point", f"{gx},{gy}"]},
        {"id": "region_image", "args": ["region", "{image}", "--rect", "0,0,8,8"]},
        {"id": "region_video", "args": ["region", "{video}", "--frame", "15", "--rect", "4,4,10,10"]},
        {"id": "timeline_points", "args": ["timeline", "{video}", "--point", f"{gx},{gy}", "--point", "2,28", "--csv", "{out:timeline.csv}", "--output", "{out:timeline.png}"]},
        {"id": "xt", "args": ["xt", "{video}", "--y", str(RED_Y), "--output", "{out:xt.png}"]},
        {"id": "yt", "args": ["yt", "{video}", "--x", str(gx), "--output", "{out:yt.png}"]},
        {"id": "changes_rect", "args": ["changes", "{video}", "--rect", "0,0,32,32", "--top", "3"]},
        {"id": "changes_point", "args": ["changes", "{video}", "--point", f"{gx},{gy}", "--top", "2"]},
        {"id": "changes_grid", "args": ["changes", "{video}", "--grid", "0,0,32,32", "--step", "4", "--top", "2"]},
        {"id": "changes_range", "args": ["changes", "{video}", "--point", "24,16", "--start-frame", "0", "--end-frame", "10", "--csv", "{out:changes.csv}"]},
        {"id": "reduce_std", "args": ["reduce", "{noise}", "--op", "std", "--output", "{out:std.png}"]},
        {"id": "reduce_mean", "args": ["reduce", "{video}", "--op", "mean", "--output", "{out:mean.png}"]},
        {"id": "compare", "args": ["compare", "{video}", "--frame-a", "0", "--frame-b", "5", "--output", "{out:diff.png}"]},
        {"id": "sheet", "args": ["sheet", "{video}", "--count", "4", "--output", "{out:sheet.png}"]},
        {"id": "scan", "args": ["scan", "{video}", "--sheet-count", "4", "--sheet-output", "{out:scan_sheet.png}", "--curve-output", "{out:scan_curve.png}"]},
        {"id": "spectrum_rect", "args": ["spectrum", "{blink}", "--rect", blink_rect]},
        {"id": "spectrum_luma", "args": ["spectrum", "{blink}", "--source", "luma"]},
        {"id": "spectrum2d", "args": ["spectrum2d", "{blink}", "--frame", "0", "--output", "{out:fft2.png}"]},
        {"id": "flow_pair", "args": ["flow", "{motion}", "--frame-a", "0", "--frame-b", "1", "--flow-output", "{out:flow.png}", "--magnitude-output", "{out:mag.png}"]},
        {"id": "workflow_quality", "args": ["workflow", "{video}", "--mode", "quality_overview"]},
        {"id": "evidence_image", "args": ["evidence", "{image}"]},
    ]


def _expand(args: list[str], assets: dict[str, Path], out_dir: Path) -> tuple[list[str], dict[str, Path]]:
    expanded: list[str] = []
    outputs: dict[str, Path] = {}
    for arg in args:
        if arg.startswith("{out:") and arg.endswith("}"):
            name = arg[5:-1]
            outputs[name] = out_dir / name
            expanded.append(str(outputs[name]))
        elif arg.startswith("{") and arg.endswith("}") and arg[1:-1] in assets:
            expanded.append(str(assets[arg[1:-1]]))
        else:
            expanded.append(arg)
    return expanded, outputs


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items()) if not _volatile(k)}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return repr(value)
        return round(value, 9)
    return value


def _file_digest(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.suffix.lower() == ".png":
        from PIL import Image
        with Image.open(path) as image:
            pixels = image.tobytes()
            return f"png:{image.mode}:{image.size[0]}x{image.size[1]}:" + hashlib.sha256(pixels).hexdigest()
    data = path.read_bytes()
    if path.suffix.lower() == ".csv":
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def run_case(case: dict[str, Any], assets: dict[str, Path], out_dir: Path) -> dict[str, Any]:
    args, outputs = _expand(case["args"], assets, out_dir)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pixelprobe", *args, "--json"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=out_dir,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"unparsed_stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:]}
    return {
        "exit_code": proc.returncode,
        "data": _normalize(payload.get("data", payload)),
        "files": {name: _file_digest(path) for name, path in sorted(outputs.items())},
    }


def _compare(expected: Any, actual: Any, where: str, problems: list[str]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                problems.append(f"{where}.{key}：缺少字段")
            elif key not in expected:
                problems.append(f"{where}.{key}：多出字段")
            else:
                _compare(expected[key], actual[key], f"{where}.{key}", problems)
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            problems.append(f"{where}：长度 {len(expected)} → {len(actual)}")
            return
        for index, (e, a) in enumerate(zip(expected, actual)):
            _compare(e, a, f"{where}[{index}]", problems)
    elif isinstance(expected, float) or isinstance(actual, float):
        if not (isinstance(expected, (int, float)) and isinstance(actual, (int, float))) or abs(
            float(expected) - float(actual)
        ) > FLOAT_TOLERANCE:
            problems.append(f"{where}：{expected!r} → {actual!r}")
    elif expected != actual:
        problems.append(f"{where}：{expected!r} → {actual!r}")


def capture(only: set[str] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pixelprobe-golden-") as tmp:
        root = Path(tmp)
        assets = build_assets(root / "assets")
        results: dict[str, Any] = {}
        for case in build_cases():
            if only and case["id"] not in only:
                continue
            out_dir = root / "out" / case["id"]
            out_dir.mkdir(parents=True)
            results[case["id"]] = run_case(case, assets, out_dir)
            print(f"  {case['id']}: exit={results[case['id']]['exit_code']}", file=sys.stderr)
        return results


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="生成或刷新基线")
    mode.add_argument("--check", action="store_true", help="与基线比对")
    parser.add_argument("--only", nargs="*", help="只运行指定用例 id")
    options = parser.parse_args()
    only = set(options.only) if options.only else None
    actual = capture(only)
    if options.write:
        if only:
            merged = json.loads(GOLDEN_PATH.read_text(encoding="utf-8")) if GOLDEN_PATH.exists() else {}
            merged.update(actual)
            actual = merged
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8",
        )
        print(f"已写入 {len(actual)} 个用例：{GOLDEN_PATH.relative_to(ROOT)}")
        return 0
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []
    for case_id, result in actual.items():
        if case_id not in expected:
            problems.append(f"{case_id}：基线中没有这个用例")
            continue
        _compare(expected[case_id], result, case_id, problems)
    if problems:
        print(f"与基线不一致的地方共 {len(problems)} 处：")
        for line in problems[:200]:
            print("  " + line)
        return 1
    print(f"{len(actual)} 个用例全部与基线一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
