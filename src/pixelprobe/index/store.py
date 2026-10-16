"""媒体索引的本机存储：按快速指纹分目录、原子提交、活动引用保护。

目录结构（每份索引一个目录，提交后不再修改）：

    <root>/<指纹前 32 位>/manifest.json   版本、媒体信息、快速指纹、分段、缩略参数
    <root>/<指纹前 32 位>/frames.npz      L1 每帧体检表
    <root>/<指纹前 32 位>/thumbs.npy      L3 缩略立方体
    <root>/<指纹前 32 位>/thumb_frames.npy

索引只是可删除的加速数据，不是证据。媒体文件变化后快速指纹改变，旧索引自然不再命中；
`prune` 按最后使用时间清理，`clear` 全部删除，二者都在确认没有活动引用后才执行。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from pixelprobe.engine.cache_lifecycle import gate, reference, require_idle
from pixelprobe.index.builder import FRAME_FIELDS, BuiltIndex
from pixelprobe.models.errors import InvalidRangeError, OutputWriteError
from pixelprobe.models.media_info import MediaInfo
from pixelprobe.utils.file_lock import require_plain
from pixelprobe.utils.fingerprint import QUICK_FINGERPRINT_VERSION, quick_fingerprint

# v2：L1 增加 blockiness。旧版本索引视为不存在（命令退回解码），重建时整体替换
INDEX_FORMAT_VERSION = "pixelprobe-media-index-v2"
_KEY_LENGTH = 32


def default_data_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "PixelProbe"


INDEX_DIR_ENV = "PIXELPROBE_INDEX_DIR"


def default_index_root() -> Path:
    """索引目录：环境变量 PIXELPROBE_INDEX_DIR 优先，否则为本机数据目录下的 index。"""
    configured = os.environ.get(INDEX_DIR_ENV)
    if configured:
        return Path(configured)
    return default_data_root() / "index"


_ERROR_NOT_SAME_DEVICE = 17
# manifest 必须最后写入：读取方以 manifest 存在且指纹匹配判定提交完成
_INDEX_FILES = ("frames.npz", "thumbs.npy", "thumb_frames.npy", "manifest.json")


def _commit_directory(temporary: Path, target: Path) -> None:
    """改名提交；个别加密或被过滤驱动重定向的目录拒绝同目录改名时，退回逐文件复制。

    复制在调用方持有的 gate 内进行，manifest 最后落盘，所以中途失败只会留下读取方视为
    "不存在"的半成品，并在这里立即清掉。
    """
    try:
        os.replace(temporary, target)
        return
    except OSError as exc:
        if getattr(exc, "winerror", None) != _ERROR_NOT_SAME_DEVICE:
            raise
    target.mkdir()
    try:
        for name in _INDEX_FILES:
            shutil.copyfile(temporary / name, target / name)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    shutil.rmtree(temporary, ignore_errors=True)


@dataclass
class MediaIndex:
    """已提交的索引。L1/L2 已读入内存，L3 缩略立方体按需读取。"""

    directory: Path
    manifest: dict
    frames: dict[str, np.ndarray]
    _thumbs: np.ndarray | None = field(default=None, repr=False)
    _thumb_frames: np.ndarray | None = field(default=None, repr=False)

    @property
    def info(self) -> MediaInfo:
        return MediaInfo.model_validate(self.manifest["media_info"])

    @property
    def segments(self) -> list[dict]:
        return list(self.manifest["segments"])

    @property
    def frame_count(self) -> int:
        return int(len(self.frames["frame"]))

    def thumbs(self) -> tuple[np.ndarray, np.ndarray]:
        """返回 (缩略帧 [N,h,w,3] uint8, 对应帧号 [N])。"""
        if self._thumbs is None:
            with reference(self.directory.parent):
                self._thumbs = np.load(self.directory / "thumbs.npy", allow_pickle=False)
                self._thumb_frames = np.load(
                    self.directory / "thumb_frames.npy", allow_pickle=False,
                )
        return self._thumbs, self._thumb_frames

    def summary(self) -> dict:
        return {
            "format": self.manifest["format"],
            "created_at": self.manifest["created_at"],
            "frame_count": self.frame_count,
            "duration_seconds": float(self.frames["time_seconds"][-1]),
            "segment_count": len(self.manifest["segments"]),
            "thumb_size": self.manifest["thumb_size"],
            "thumb_stride": self.manifest["thumb_stride"],
            "thumb_count": self.manifest["thumb_count"],
            "size_bytes": sum(
                path.stat().st_size for path in self.directory.iterdir() if path.is_file()
            ),
        }


class IndexStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_index_root()

    # ---- 查找 ----

    def key_for(self, media: Path) -> tuple[str, str]:
        fingerprint = quick_fingerprint(Path(media))
        return fingerprint[:_KEY_LENGTH], fingerprint

    def lookup(self, media: Path) -> MediaIndex | None:
        """返回与媒体当前内容匹配的索引；没有、已过期或损坏时返回 None。"""
        if not self.root.is_dir():
            return None
        key, fingerprint = self.key_for(media)
        directory = self.root / key
        if not directory.is_dir():
            return None
        with reference(self.root):
            try:
                manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                if (
                    manifest.get("format") != INDEX_FORMAT_VERSION
                    or manifest.get("fingerprint") != fingerprint
                ):
                    return None
                with np.load(directory / "frames.npz", allow_pickle=False) as archive:
                    frames = {name: archive[name] for name in FRAME_FIELDS}
            except (OSError, ValueError, KeyError):
                return None
        self._touch(directory)
        return MediaIndex(directory=directory, manifest=manifest, frames=frames)

    # ---- 写入 ----

    def save(self, media: Path, built: BuiltIndex) -> MediaIndex:
        """原子提交：先写临时目录，完整写完后改名；同指纹索引已存在时保留已有的一份。"""
        try:
            return self._save(media, built)
        except OSError as exc:
            raise OutputWriteError(
                f"索引写入失败：{exc.strerror or type(exc).__name__}",
                hint=f"可设置环境变量 {INDEX_DIR_ENV} 指向其他可写目录后重试",
            ) from exc

    def _save(self, media: Path, built: BuiltIndex) -> MediaIndex:
        key, fingerprint = self.key_for(media)
        self.root.mkdir(parents=True, exist_ok=True)
        require_plain(self.root)
        temporary = self.root / f".tmp-{key}-{uuid.uuid4().hex}"
        temporary.mkdir()
        try:
            np.savez(temporary / "frames.npz", **built.frames)
            np.save(temporary / "thumbs.npy", built.thumbs, allow_pickle=False)
            np.save(temporary / "thumb_frames.npy", built.thumb_frames, allow_pickle=False)
            manifest = {
                "format": INDEX_FORMAT_VERSION,
                "fingerprint_kind": QUICK_FINGERPRINT_VERSION,
                "fingerprint": fingerprint,
                "media_name": Path(media).name,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "media_info": built.info.model_dump(mode="json"),
                "segments": built.segments,
                "segment_threshold": built.segment_threshold,
                "thumb_size": list(built.thumbs.shape[1:3]),
                "thumb_stride": built.thumb_stride,
                "thumb_count": int(built.thumbs.shape[0]),
            }
            manifest["media_info"]["path"] = Path(media).name
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8",
            )
            target = self.root / key
            with gate(self.root):
                if target.exists():
                    if self._valid(target, fingerprint):
                        # 同一内容已有有效索引（可能被并发生成），保留已提交的那份
                        shutil.rmtree(temporary, ignore_errors=True)
                    else:
                        require_idle(self.root)
                        shutil.rmtree(target)
                if temporary.exists():
                    _commit_directory(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        index = self.lookup(media)
        if index is None:
            raise InvalidRangeError("索引写入后无法读回，请检查索引目录是否被其他程序修改")
        return index

    # ---- 维护 ----

    def entries(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(
            path for path in self.root.iterdir()
            if path.is_dir() and len(path.name) == _KEY_LENGTH and not path.name.startswith(".")
        )

    def remove(self, media: Path) -> bool:
        key, _ = self.key_for(media)
        target = self.root / key
        with gate(self.root):
            require_idle(self.root)
            if not target.is_dir():
                return False
            shutil.rmtree(target)
            return True

    def prune(self, *, max_age_seconds: float | None = None, dry_run: bool = True) -> dict:
        """删除超过 max_age_seconds 未使用的索引（None 表示全部）；默认只预览。"""
        now = time.time()
        selected: list[Path] = []
        total = 0
        for entry in self.entries():
            used = (entry / ".last_used").stat().st_mtime if (entry / ".last_used").exists() \
                else entry.stat().st_mtime
            if max_age_seconds is None or now - used >= max_age_seconds:
                selected.append(entry)
                total += sum(path.stat().st_size for path in entry.iterdir() if path.is_file())
        if not dry_run and selected:
            with gate(self.root):
                require_idle(self.root)
                for entry in selected:
                    shutil.rmtree(entry)
        return {
            "index_dir": str(self.root),
            "dry_run": dry_run,
            "entries": len(selected),
            "bytes": total,
        }

    @staticmethod
    def _valid(directory: Path, fingerprint: str) -> bool:
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if (
            manifest.get("format") != INDEX_FORMAT_VERSION
            or manifest.get("fingerprint") != fingerprint
            or not all((directory / name).is_file() for name in ("thumbs.npy", "thumb_frames.npy"))
        ):
            return False
        try:
            with np.load(directory / "frames.npz", allow_pickle=False) as archive:
                return all(name in archive.files for name in FRAME_FIELDS)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _touch(directory: Path) -> None:
        try:
            (directory / ".last_used").touch()
        except OSError:
            pass
