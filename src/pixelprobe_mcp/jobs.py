"""有界 FIFO 调度、任务状态持久化与结果领取。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
import re
from hashlib import sha256
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel

from pixelprobe_mcp.config import ServerConfig, PathAccessError, _is_reparse_point
from pixelprobe_mcp.diagnostics import actionable_error, record_diagnostic
from pixelprobe_mcp.models import ToolEnvelope
from pixelprobe_mcp.resource_plan import RUNTIME_BYTES
from pixelprobe_mcp.worker import WorkerError, run_operation_sync


class QueueFullError(RuntimeError):
    pass


class ResourceBudgetExceededError(RuntimeError):
    """单任务声明的资源预算超过全局预算。"""
    pass


class JobNotFoundError(KeyError):
    pass


MIB = 1024 * 1024
OPERATION_DEFAULT_RESOURCES: dict[str, tuple[int, int]] = {
    "list_artifacts": (32 * MIB, 0),
    "read_artifact": (64 * MIB, 0),
}
# 写结果包的能力预留临时空间；生成正式表示需要较大的共享帧存储
# 工作进程的预算计划 = 运行时余量（RUNTIME_BYTES）+ 分析内存；CLI 的分析预算为 256 MiB，
# 这里必须额外预留运行时余量，否则 100 万像素以上的图片会在预检阶段被误拒
_ANALYSIS_MIB = 256
_WORKFLOW_MEMORY = (_ANALYSIS_MIB + RUNTIME_BYTES // MIB) * MIB
CAPABILITY_DEFAULT_RESOURCES: dict[str, tuple[int, int]] = {
    "generate": (256 * MIB, 2048 * MIB),
    "evidence": (_WORKFLOW_MEMORY, 512 * MIB),
    "first_change": (_WORKFLOW_MEMORY, 512 * MIB),
    "compare_media": (_WORKFLOW_MEMORY, 512 * MIB),
    "quality_overview": (_WORKFLOW_MEMORY, 512 * MIB),
    # 走引擎的能力：解码帧存放在共享帧存储中，需要临时空间
    **{name: (256 * MIB, 2048 * MIB) for name in (
        "timeline", "xt", "yt", "reduce", "spectrum", "spectrum2d", "flow",
    )},
}


def estimate_operation_resources(
    operation: str, payload: dict[str, Any] | None = None,
) -> tuple[int, int]:
    if operation == "capability" and isinstance(payload, dict):
        default_mem, default_temp = CAPABILITY_DEFAULT_RESOURCES.get(
            str(payload.get("capability")), (64 * MIB, 0),
        )
    else:
        default_mem, default_temp = OPERATION_DEFAULT_RESOURCES.get(operation, (64 * MIB, 0))
    if payload and isinstance(payload, dict):
        if "media_path" in payload or "bundle_path" in payload:
            # 引擎、NumPy、原生库及 Python 本身也消耗内存；实际计划在受限子进程内细化。
            default_mem = max(default_mem, 256 * 1024 * 1024)
            default_temp = max(default_temp, 4 * 1024 * 1024)
        custom_mem = payload.get("required_memory_bytes")
        custom_temp = payload.get("required_temporary_bytes")
        if custom_mem is not None:
            default_mem = max(default_mem, int(custom_mem))
        if custom_temp is not None:
            default_temp = max(default_temp, int(custom_temp))
    return default_mem, default_temp


_JOB_IMAGE = re.compile(r"[0-9a-f]{32}\.\d{1,3}\.png")


def job_files(root: Path, job_id: str) -> list[Path]:
    """某个任务的全部已知文件：记录、结果与按序号保存的图片（不做宽泛通配删除）。"""
    files = [root / f"{job_id}{suffix}" for suffix in (".json", ".request.json", ".result.json", ".png")]
    files.extend(
        path for path in root.glob(f"{job_id}.*.png")
        if _JOB_IMAGE.fullmatch(path.name) and path.name.startswith(job_id)
    )
    return files


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class Job:
    job_id: str
    operation: str
    payload: dict[str, Any]
    timeout_seconds: float
    persistent: bool
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool | None = None
    suggested_action: str | None = None
    diagnostic_id: str | None = None
    result: Any = None
    result_ready: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future | None = None
    queued_at: float = field(default_factory=time.monotonic)
    queue_timer: threading.Timer | None = None
    required_memory_bytes: int = 0
    required_temporary_bytes: int = 0
    resource_reserved: bool = False
    resource_plan: dict[str, Any] | None = None
    resource_usage: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "operation": self.operation, "status": self.status,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "result_ready": self.result_ready,
            "error": self.error, "error_code": self.error_code,
            "retryable": self.retryable,
            "suggested_action": self.suggested_action,
            "diagnostic_id": self.diagnostic_id,
            "capability": self.payload.get("capability"),
            "resource_plan": self.resource_plan,
            "resource_usage": self.resource_usage,
        }


class JobManager:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._executor = ThreadPoolExecutor(
            max_workers=config.workers, thread_name_prefix="pixelprobe-job",
        )
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        self._outstanding = 0
        self._allocated_memory_bytes = 0
        self._allocated_temporary_bytes = 0
        self._root: Path | None = None
        self._closed = False
        self._owner_id = uuid.uuid4().hex
        self._owner_lease = None

    @property
    def allocated_memory_bytes(self) -> int:
        with self._lock:
            return self._allocated_memory_bytes

    @property
    def allocated_temporary_bytes(self) -> int:
        with self._lock:
            return self._allocated_temporary_bytes

    def _ensure_store(self) -> Path:
        with self._lock:
            root = self.config.prepare_artifact_root() / ".jobs"
            if root.is_symlink() or _is_reparse_point(root):
                raise PathAccessError("任务目录不能是符号链接或重解析点")
            root.mkdir(exist_ok=True)
            if self._root is None:
                from pixelprobe_mcp.storage_lock import StorageLease
                with StorageLease(root / ".maintenance.lock"):
                    self._owner_lease = StorageLease(root / f".owner-{self._owner_id}.lock")
                    self._root = root
                    self._recover_and_clean()
            return self._root

    def _record_path(self, job_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise JobNotFoundError(job_id)
        return self._ensure_store() / f"{job_id}.json"

    def _write_json(self, target: Path, value: dict[str, Any]) -> None:
        temporary = target.with_suffix(f".tmp-{uuid.uuid4().hex}")
        data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self._check_record_write(target, temporary, len(data))
        try:
            with temporary.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _check_record_write(self, target: Path, temporary: Path, size: int) -> None:
        from pixelprobe.utils.resource_budget import ResourceBudget
        from pixelprobe_mcp.resource_plan import CONTROL_BYTES
        budget = ResourceBudget(self.config.total_memory_bytes,
                                min(CONTROL_BYTES, self.config.total_temporary_bytes))
        identity = target.name.split(".")[0]
        for existing in job_files(target.parent, identity):
            if existing.is_file():
                budget.reserve(existing, existing.stat().st_size)
        budget.reserve(temporary, size)

    def _persist(self, job: Job) -> None:
        if job.persistent:
            self._write_json(self._record_path(job.job_id), job.public())

    def _recover_and_clean(self) -> None:
        assert self._root is not None
        if self._other_storage_owner():
            return
        for path in self._root.glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{32}", path.stem) is None:
                continue
            if path.is_symlink() or _is_reparse_point(path):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("job_id") != path.stem:
                    continue
                if value.get("status") in {"queued", "running"}:
                    value.update(status="interrupted", finished_at=_now(), result_ready=False)
                    self._write_json(path, value)
            except (OSError, ValueError, TypeError):
                continue

    def _other_storage_owner(self) -> bool:
        from pixelprobe_mcp.storage_lock import StorageLease
        assert self._root is not None
        for path in self._root.glob(".owner-*.lock"):
            if path.name == f".owner-{self._owner_id}.lock":
                continue
            try:
                with StorageLease(path):
                    pass
            except (OSError, ValueError):
                return True
        return False

    def _release_storage_owner(self) -> None:
        if self._closed and self._outstanding == 0 and self._owner_lease is not None:
            self._owner_lease.close()
            self._owner_lease = None

    def submit(
        self, operation: str, payload: dict[str, Any], *, timeout_seconds: float,
        persistent: bool,
    ) -> Job:
        with self._lock:
            if self._closed:
                raise RuntimeError("任务调度器已关闭")
            memory, temporary = estimate_operation_resources(operation, payload)
            if "media_path" in payload or "bundle_path" in payload:
                memory = max(memory, self.config.total_memory_bytes // self.config.workers)
                if self.config.total_memory_bytes >= 128 * 1024 * 1024:
                    memory = min(memory, self.config.total_memory_bytes)
                if self.config.total_temporary_bytes >= 4 * 1024 * 1024:
                    temporary = min(temporary, self.config.total_temporary_bytes)
            if memory > self.config.total_memory_bytes or temporary > self.config.total_temporary_bytes:
                raise ResourceBudgetExceededError("MCP_RESOURCE_LIMIT：单任务资源需求超过总预算")
            capacity = self.config.workers + self.config.queue_size
            if self._outstanding >= capacity:
                raise QueueFullError("MCP_QUEUE_FULL：等待队列已满；请稍后重试")
            job = Job(uuid.uuid4().hex, operation, payload, timeout_seconds, persistent)
            job.required_memory_bytes = memory
            job.required_temporary_bytes = temporary
            self._jobs[job.job_id] = job
            self._outstanding += 1
            try:
                self._persist(job)
                job.future = self._executor.submit(self._run, job)
            except Exception:
                self._jobs.pop(job.job_id, None)
                self._outstanding -= 1
                raise
            job.queue_timer = threading.Timer(
                max(0.0, self.config.queue_timeout_seconds - (time.monotonic() - job.queued_at)),
                self._expire_queued, args=(job,),
            )
            job.queue_timer.daemon = True
            job.queue_timer.start()
            return job

    def _queue_timeout(self, job: Job) -> None:
        self._set_error(
            job, status="timeout", code="MCP_QUEUE_TIMEOUT",
            message=f"排队超过 {self.config.queue_timeout_seconds:g} 秒",
            retryable=True, action="稍后重试或减少同时提交的任务",
        )

    def _expire_queued(self, job: Job) -> None:
        with self._lock:
            if job.status != "queued" or job.future is None or not job.future.cancel():
                return
            try:
                self._queue_timeout(job)
                job.finished_at = _now()
                self._persist(job)
            except (OSError, TypeError, ValueError) as exc:
                self._set_error(
                    job, status="failed", code="MCP_STATE_COMMIT_FAILED",
                    message="排队超时状态无法持久化", retryable=True,
                    action="检查 Artifact 目录权限和磁盘空间后重试", exception=exc,
                )
            finally:
                self._outstanding -= 1
                self._condition.notify_all()

    def _apply_resource_plan(self, job: Job, plan: dict[str, Any]) -> None:
        with self._lock:
            memory, disk = int(plan["memory_bytes"]), int(plan["temporary_bytes"])
            if (memory < 0 or disk < 0 or memory > job.required_memory_bytes
                    or disk > job.required_temporary_bytes):
                raise WorkerError("MCP_RESOURCE_LIMIT：工作进程计划超出已预留预算")
            self._allocated_memory_bytes -= job.required_memory_bytes - memory
            self._allocated_temporary_bytes -= job.required_temporary_bytes - disk
            job.required_memory_bytes, job.required_temporary_bytes = memory, disk
            job.resource_plan = dict(plan)
            self._persist(job)
            self._condition.notify_all()

    def _run(self, job: Job) -> Any:
        try:
            with self._lock:
                # 等待资源仍属于排队，按提交顺序准入。
                while not job.cancel_event.is_set():
                    remaining = self.config.queue_timeout_seconds - (time.monotonic() - job.queued_at)
                    if remaining <= 0:
                        break
                    first = next((item for item in self._jobs.values() if item.status == "queued"), None)
                    fits = (
                        self._allocated_memory_bytes + job.required_memory_bytes <= self.config.total_memory_bytes
                        and self._allocated_temporary_bytes + job.required_temporary_bytes <= self.config.total_temporary_bytes
                    )
                    if first is job and fits:
                        break
                    self._condition.wait(remaining)
                if job.queue_timer is not None:
                    job.queue_timer.cancel()
                if time.monotonic() - job.queued_at >= self.config.queue_timeout_seconds:
                    self._queue_timeout(job)
                    return None
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    return None
                job.status, job.started_at = "running", _now()
                self._allocated_memory_bytes += job.required_memory_bytes
                self._allocated_temporary_bytes += job.required_temporary_bytes
                job.resource_reserved = True
                self._condition.notify_all()
            self._persist(job)
            try:
                result = run_operation_sync(
                    self.config, job.operation, job.payload,
                    timeout_seconds=job.timeout_seconds, cancel_event=job.cancel_event,
                    memory_limit_bytes=job.required_memory_bytes,
                    temporary_limit_bytes=job.required_temporary_bytes,
                    on_plan=lambda plan: self._apply_resource_plan(job, plan),
                    on_usage=lambda usage: setattr(job, "resource_usage", usage),
                )
            except TimeoutError:
                self._set_error(
                    job, status="timeout", code="MCP_TIMEOUT",
                    message="执行超时，工作进程已回收", retryable=True,
                    action="缩小媒体范围或提高任务时限",
                )
            except WorkerError as exc:
                if str(exc).startswith("MCP_CANCELLED"):
                    job.status = "cancelled"
                else:
                    raw = str(exc)
                    code, _, message = raw.partition("：")
                    self._set_error(
                        job, status="failed", code=code or "MCP_WORKER_FAILED",
                        message=message or raw,
                        retryable=code in {
                            "MCP_WORKER_CRASHED", "MCP_INTERNAL_ERROR",
                            "MEDIA_CHANGED_DURING_ANALYSIS",
                        },
                        action="检查诊断日志、输入文件与资源配置后重试",
                        exception=exc,
                    )
            except Exception as exc:
                self._set_error(
                    job, status="failed", code="MCP_WORKER_FAILED",
                    message="工作进程执行失败", retryable=True,
                    action="检查诊断日志后重试", exception=exc,
                )
            else:
                job.result = result
                if job.persistent:
                    try:
                        self._save_result(job)
                    except (OSError, TypeError, ValueError):
                        self._set_error(
                            job, status="failed", code="MCP_RESULT_COMMIT_FAILED",
                            message="结果未能原子提交", retryable=True,
                            action="检查 Artifact 目录权限和磁盘空间后重试",
                        )
                        job.result = None
                        job.result_ready = False
            return job.result
        except (OSError, TypeError, ValueError) as exc:
            self._set_error(
                job, status="failed", code="MCP_STATE_COMMIT_FAILED",
                message="任务状态无法持久化", retryable=True,
                action="检查 Artifact 目录权限和磁盘空间后重试", exception=exc,
            )
            return None
        finally:
            with self._lock:
                try:
                    # 取消与最终状态提交使用同一把锁；取消先到则不发布结果。
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                        job.result_ready = False
                        job.result = None
                    elif job.status == "running":
                        job.status, job.result_ready = "succeeded", True
                    job.finished_at = _now()
                    self._persist(job)
                except (OSError, TypeError, ValueError) as exc:
                    job.result_ready = False
                    job.result = None
                    self._set_error(
                        job, status="failed", code="MCP_STATE_COMMIT_FAILED",
                        message="最终任务状态未能原子提交", retryable=True,
                        action="检查 Artifact 目录权限和磁盘空间后重试", exception=exc,
                    )
                finally:
                    if job.resource_reserved:
                        self._allocated_memory_bytes -= job.required_memory_bytes
                        self._allocated_temporary_bytes -= job.required_temporary_bytes
                        job.resource_reserved = False
                    self._outstanding -= 1
                    self._condition.notify_all()
                    self._release_storage_owner()

    def _set_error(
        self,
        job: Job,
        *,
        status: str,
        code: str,
        message: str,
        retryable: bool,
        action: str,
        exception: BaseException | None = None,
    ) -> None:
        diagnostic_id = record_diagnostic(
            self.config, code=code, operation=job.operation,
            exception_type=type(exception).__name__ if exception else None,
        )
        job.status = status
        job.error_code = code
        job.retryable = retryable
        job.suggested_action = action
        job.diagnostic_id = diagnostic_id
        job.error = actionable_error(
            code, message, retryable=retryable, action=action,
            diagnostic_id=diagnostic_id,
        )

    @staticmethod
    def split_result(value: Any) -> tuple[Any, list[tuple[str, bytes]]]:
        """工作进程结果可能是 (响应, [(图片名, PNG 字节), ...]) 或单独的响应。"""
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], list):
            return value[0], value[1]
        return value, []

    def _save_result(self, job: Job) -> None:
        root = self._ensure_store()
        result, images = self.split_result(job.result)
        if isinstance(result, BaseModel):
            result = result.model_dump(mode="json")
        # 先写图片，再写引用它们的结果记录；结果记录写成功才算提交
        for number, (_, image) in enumerate(images):
            target = root / f"{job.job_id}.{number}.png"
            temporary = target.with_suffix(f".tmp-{uuid.uuid4().hex}")
            self._check_record_write(target, temporary, len(image))
            try:
                with temporary.open("xb") as output:
                    output.write(image)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        self._write_json(root / f"{job.job_id}.result.json", {
            "result": result,
            "images": [
                {"name": name, "sha256": sha256(image).hexdigest()} for name, image in images
            ],
        })

    async def wait(self, job: Job) -> Any:
        try:
            while True:
                with self._lock:
                    if job.future is None or job.future.done():
                        if job.status == "succeeded":
                            return job.result
                        raise WorkerError(job.error or f"MCP_{job.status.upper()}：任务未完成")
                await anyio.sleep(0.01)
        except anyio.get_cancelled_exc_class():
            self.cancel(job.job_id)
            raise
        finally:
            if not job.persistent:
                with self._lock:
                    self._jobs.pop(job.job_id, None)

    def get(self, job_id: str) -> Job | dict[str, Any]:
        with self._lock:
            if job_id in self._jobs:
                return self._jobs[job_id]
        path = self._record_path(job_id)
        if not path.is_file():
            raise JobNotFoundError(job_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._cancel(job_id)

    def _cancel(self, job_id: str) -> dict[str, Any]:
        value = self.get(job_id)
        if isinstance(value, dict):
            return value
        if value.status in {"queued", "running"}:
            value.cancel_event.set()
            self._condition.notify_all()
            if value.future is not None and value.future.cancel():
                if value.queue_timer is not None:
                    value.queue_timer.cancel()
                value.status, value.finished_at = "cancelled", _now()
                with self._lock:
                    self._outstanding -= 1
                self._persist(value)
        return value.public()

    def maintain_storage(self, *, dry_run: bool = True) -> dict[str, Any]:
        from pixelprobe_mcp.maintenance import maintain
        from pixelprobe_mcp.storage_lock import StorageLease
        with self._lock:
            root = self._ensure_store()
            with StorageLease(root / ".maintenance.lock"):
                return maintain(self, dry_run=dry_run)

    def result(self, job_id: str) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
        with self._lock:
            return self._result(job_id)

    def _result(self, job_id: str) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
        value = self.get(job_id)
        public = value.public() if isinstance(value, Job) else value
        if public["status"] != "succeeded" or not public["result_ready"]:
            raise WorkerError("MCP_JOB_NOT_READY：任务尚未成功完成")
        if isinstance(value, Job) and not value.persistent:
            result, images = self.split_result(value.result)
            if isinstance(result, BaseModel):
                result = result.model_dump(mode="json")
            return result, images
        root = self._ensure_store()
        result_path = root / f"{job_id}.result.json"
        try:
            if result_path.is_symlink() or _is_reparse_point(result_path):
                raise ValueError("结果文件不能是链接")
            stored = json.loads(result_path.read_text(encoding="utf-8"))
            images: list[tuple[str, bytes]] = []
            for number, record in enumerate(stored.get("images", [])):
                path = root / f"{job_id}.{number}.png"
                if path.is_symlink() or _is_reparse_point(path):
                    raise ValueError("结果文件不能是链接")
                image = path.read_bytes()
                if sha256(image).hexdigest() != record["sha256"]:
                    raise ValueError("图片结果校验失败")
                images.append((str(record["name"]), image))
            return stored["result"], images
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorkerError("MCP_RESULT_INVALID：结果缺失或损坏，请重新提交任务") from exc

    def shutdown(self, *, wait: bool = False) -> None:
        """停止接收任务，并通知仍在排队或运行的工作进程退出。"""
        with self._lock:
            self._closed = True
            for job in list(self._jobs.values()):
                try:
                    self._cancel(job.job_id)
                except OSError:
                    # 单条状态写入失败不能阻止其他工作进程收到取消。
                    continue
        self._executor.shutdown(wait=wait, cancel_futures=False)
        with self._lock:
            self._release_storage_owner()
