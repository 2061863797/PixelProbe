"""PixelProbe MCP 的本地路径与载荷安全配置。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
import re
import json
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
    workers: int = 2
    queue_size: int = 16
    queue_timeout_seconds: float = 30.0
    job_retention_days: int = 7
    total_memory_bytes: int = 512 * 1024 * 1024
    total_temporary_bytes: int = 4 * 1024 * 1024 * 1024

    @classmethod
    def from_environment(
        cls, *, allowed_roots: tuple[str, ...] | None = None,
        artifact_root: str | None = None,
        workers: int | None = None,
        queue_size: int | None = None,
        total_memory_bytes: int | None = None,
        total_temporary_bytes: int | None = None,
    ) -> "ServerConfig":
        raw_roots = (os.environ.get("PIXELPROBE_MCP_ROOTS") or "").strip()
        # 未配置时：Claude Code 为 MCP 进程设置的项目根目录优先，其次是启动目录
        default_root = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip() or os.getcwd()
        values = allowed_roots if allowed_roots is not None else (
            raw_roots.split(os.pathsep) if raw_roots else [default_root]
        )
        roots = tuple(
            dict.fromkeys(
                Path(value).expanduser().resolve(strict=True)
                for value in values if value.strip()
            )
        )
        if not roots or any(not root.is_dir() for root in roots):
            raise PathAccessError("PIXELPROBE_MCP_ROOTS 必须包含现有目录")
        configured_output = artifact_root if artifact_root is not None else os.environ.get(
            "PIXELPROBE_MCP_ARTIFACT_ROOT"
        )
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
        resolved_workers = workers if workers is not None else int(
            os.environ.get("PIXELPROBE_MCP_WORKERS", "2")
        )
        resolved_queue = queue_size if queue_size is not None else int(
            os.environ.get("PIXELPROBE_MCP_QUEUE_SIZE", "16")
        )
        if not 1 <= resolved_workers <= 16:
            raise ValueError("MCP workers 必须在 1～16 之间")
        if not 0 <= resolved_queue <= 256:
            raise ValueError("MCP queue_size 必须在 0～256 之间")

        def _positive_int(value: int | None, env_name: str, default: int) -> int:
            # 插件把留空的配置项传成空字符串，按未设置处理
            raw = value if value is not None else (os.environ.get(env_name) or "").strip() or str(default)
            try:
                parsed = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{env_name} 必须是整数") from exc
            if parsed < 1:
                raise ValueError(f"{env_name} 必须为正整数")
            return parsed

        # 显式参数 > 字节数 > MB（便于插件配置）> 按本机内存自动（物理内存一半与空闲 70% 取小）
        from pixelprobe.utils.resource_budget import machine_memory_budget
        default_memory = _positive_int(
            None, "PIXELPROBE_MCP_TOTAL_MEMORY_MB", max(1, machine_memory_budget() // (1024 * 1024)),
        ) * 1024 * 1024
        memory_budget = _positive_int(
            total_memory_bytes, "PIXELPROBE_MCP_TOTAL_MEMORY_BYTES", default_memory,
        )
        temporary_budget = _positive_int(
            total_temporary_bytes, "PIXELPROBE_MCP_TOTAL_TEMPORARY_BYTES",
            4 * 1024 * 1024 * 1024,
        )
        return cls(
            roots, artifact_root, max_image_bytes,
            workers=resolved_workers, queue_size=resolved_queue,
            total_memory_bytes=memory_budget,
            total_temporary_bytes=temporary_budget,
        )

    def resolve_file(self, value: str) -> Path:
        # 先按白名单判断再报"不存在"，避免借错误码探测白名单外的文件是否存在
        self._require_allowed(Path(value).expanduser().resolve(strict=False))
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            from pixelprobe.models.errors import MediaNotFoundError
            # 工作进程错误不回显调用方传入的路径或文件名
            raise MediaNotFoundError("输入文件不存在", hint="请检查路径拼写") from exc
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

    def checkpoint_directory(self, checkpoint_id: str, *, create: bool = False) -> Path:
        """仅在受管目录中访问 checkpoint，不接受用户提供的任意路径。"""
        if re.fullmatch(r"[0-9a-f]{32}", checkpoint_id) is None:
            raise PathAccessError("checkpoint ID 无效")
        root = self.prepare_artifact_root()
        for name in (".checkpoints", checkpoint_id):
            root = root / name
            if root.is_symlink() or _is_reparse_point(root):
                raise PathAccessError("checkpoint 目录不能是链接或重解析点")
            if create:
                existed = root.exists()
                root.mkdir(exist_ok=True)
                if name == checkpoint_id and not existed:
                    from pixelprobe.utils.resource_budget import write_bytes
                    write_bytes(root / ".pixelprobe-owned.json", json.dumps({
                        "owner": "pixelprobe", "kind": "checkpoint",
                        "checkpoint_id": checkpoint_id,
                    }).encode("utf-8"))
            if not root.is_dir():
                raise PathAccessError("checkpoint 目录不存在")
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
            # 与 CLI 相同的错误码；这不是白名单问题，不能报 PATH_NOT_ALLOWED
            from pixelprobe.artifacts.errors import BundleTargetExistsError
            raise BundleTargetExistsError(
                f"输出名称已存在：{name}", hint="换一个 output_name，或省略它让服务自动命名",
            )
        return target

    def _require_allowed(self, path: Path) -> None:
        if not any(_inside(path, root) for root in self.allowed_roots):
            roots = ", ".join(str(root) for root in self.allowed_roots)
            raise PathAccessError(
                f"路径不在允许范围内：{path}。可通过 PIXELPROBE_MCP_ROOTS 配置：{roots}"
            )
