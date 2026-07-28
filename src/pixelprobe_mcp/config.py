"""PixelProbe MCP 的本地路径与载荷安全配置。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path


_FINGERPRINT_BLOCK_BYTES = 64 * 1024


class PathAccessError(ValueError):
    """路径不在用户允许的 MCP 根目录中。"""


class MediaChangedError(ValueError):
    """分析期间媒体文件被替换或修改。"""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """用于在路径检查后复验同一输入文件的稳定身份。"""

    path: Path
    device: int
    inode: int
    size_bytes: int
    modified_time_ns: int
    changed_time_ns: int
    content_fingerprint: str


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_reparse_point(path: Path) -> bool:
    """识别 Windows 重解析点；POSIX 上该属性不存在时返回 False。"""
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _file_stat_values(path: Path) -> tuple[int, int, int, int, int]:
    """返回输入身份比较需要的稳定 stat 字段。"""
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _content_fingerprint(path: Path, size_bytes: int) -> str:
    """计算首尾采样指纹，补足 Windows 上可伪造的时间戳身份检查。

    这不是完整内容哈希：MCP 需要在每次读取前后都保持低固定开销，故只读取
    首尾各 64 KiB，并始终与设备、inode、大小和时间戳一起使用。
    """
    digest = sha256()
    digest.update(size_bytes.to_bytes(16, byteorder="big", signed=False))
    with path.open("rb") as source:
        head = source.read(_FINGERPRINT_BLOCK_BYTES)
        digest.update(len(head).to_bytes(8, byteorder="big", signed=False))
        digest.update(head)
        if size_bytes > _FINGERPRINT_BLOCK_BYTES:
            source.seek(max(0, size_bytes - _FINGERPRINT_BLOCK_BYTES))
            tail = source.read(_FINGERPRINT_BLOCK_BYTES)
            digest.update(len(tail).to_bytes(8, byteorder="big", signed=False))
            digest.update(tail)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ServerConfig:
    allowed_roots: tuple[Path, ...]
    artifact_root: Path
    max_image_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_environment(cls) -> "ServerConfig":
        raw_roots = os.environ.get("PIXELPROBE_MCP_ROOTS")
        values = raw_roots.split(os.pathsep) if raw_roots else [os.getcwd()]
        roots = tuple(
            dict.fromkeys(
                Path(value).expanduser().resolve(strict=True)
                for value in values if value.strip()
            )
        )
        if not roots or any(not root.is_dir() for root in roots):
            raise PathAccessError("PIXELPROBE_MCP_ROOTS 必须包含现有目录")
        configured_output = os.environ.get("PIXELPROBE_MCP_ARTIFACT_ROOT")
        artifact_root = (
            Path(configured_output).expanduser()
            if configured_output else roots[0] / ".pixelprobe-mcp" / "artifacts"
        ).resolve(strict=False)
        if not any(_inside(artifact_root, root) for root in roots):
            raise PathAccessError("PIXELPROBE_MCP_ARTIFACT_ROOT 必须位于允许根目录内")
        raw_limit = os.environ.get("PIXELPROBE_MCP_MAX_IMAGE_BYTES", "16777216")
        try:
            max_image_bytes = int(raw_limit)
        except ValueError as exc:
            raise ValueError("PIXELPROBE_MCP_MAX_IMAGE_BYTES 必须是整数") from exc
        if max_image_bytes < 1024:
            raise ValueError("PIXELPROBE_MCP_MAX_IMAGE_BYTES 必须至少为 1024")
        return cls(roots, artifact_root, max_image_bytes)

    def resolve_file(self, value: str) -> Path:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file():
            raise PathAccessError(f"不是可读文件：{value}")
        self._require_allowed(path)
        return path

    def resolve_file_identity(self, value: str) -> FileIdentity:
        """解析白名单内文件，并记录后续复验所需的身份字段。"""
        path = self.resolve_file(value)
        try:
            before = _file_stat_values(path)
            fingerprint = _content_fingerprint(path, before[2])
            after = _file_stat_values(path)
        except OSError as exc:
            raise MediaChangedError("输入媒体在身份检查期间不可再安全访问") from exc
        if after != before:
            raise MediaChangedError("输入媒体在身份检查期间发生变化")
        return FileIdentity(
            path=path,
            device=before[0],
            inode=before[1],
            size_bytes=before[2],
            modified_time_ns=before[3],
            changed_time_ns=before[4],
            content_fingerprint=fingerprint,
        )

    def verify_file_identity(self, identity: FileIdentity) -> None:
        """拒绝分析期间被替换、改写或移出白名单的输入文件。"""
        try:
            current = identity.path.resolve(strict=True)
            self._require_allowed(current)
            before = _file_stat_values(current)
            fingerprint = _content_fingerprint(current, before[2])
            after = _file_stat_values(current)
        except (OSError, PathAccessError) as exc:
            raise MediaChangedError("输入媒体在分析期间不可再安全访问") from exc
        expected = (
            identity.device,
            identity.inode,
            identity.size_bytes,
            identity.modified_time_ns,
            identity.changed_time_ns,
        )
        if (
            current != identity.path
            or before != expected
            or after != before
            or fingerprint != identity.content_fingerprint
        ):
            raise MediaChangedError("输入媒体在分析期间发生变化，结果已丢弃")

    def resolve_directory(self, value: str) -> Path:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise PathAccessError(f"不是可读目录：{value}")
        self._require_allowed(path)
        return path

    def prepare_artifact_root(self) -> Path:
        """创建写入根并在创建后重新解析，避免链接把输出导向白名单外。"""
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        root = self.artifact_root.resolve(strict=True)
        if not root.is_dir():
            raise PathAccessError(f"Artifact 根目录无效：{root}")
        self._require_allowed(root)
        return root

    def prepare_artifact_target(self, name: str) -> Path:
        """返回受控输出目标，并拒绝已有链接、重解析点或同名结果。"""
        root = self.prepare_artifact_root()
        # 该方法也可能被未来的服务入口直接调用，不能只依赖 MCP 的 Pydantic
        # pattern 校验。输出始终是 Artifact 根的一层 Bundle 目录。
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or Path(name).name != name
        ):
            raise PathAccessError("Artifact 输出名称必须是单个安全文件名")
        target = root / name
        if target.is_symlink() or _is_reparse_point(target):
            raise PathAccessError("Artifact 输出目标不能是链接或重解析点")
        if target.exists():
            raise PathAccessError("Artifact 输出名称已存在；请换一个 output_name")
        return target

    def _require_allowed(self, path: Path) -> None:
        if not any(_inside(path, root) for root in self.allowed_roots):
            roots = ", ".join(str(root) for root in self.allowed_roots)
            raise PathAccessError(
                f"路径不在允许范围内：{path}。可通过 PIXELPROBE_MCP_ROOTS 配置：{roots}"
            )
