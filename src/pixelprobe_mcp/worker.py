"""媒体操作的进程隔离执行；不接受任意可调用对象。

工作进程常驻复用：一个进程完成任务后回到空闲池，下一次调用免去启动进程、
加载依赖的开销；优先复用刚处理过同一媒体的进程（索引与系统文件缓存更热）。

安全边界与逐次 spawn 时相同：每个任务仍有独立的内存上限、临时目录、超时和协作取消；
一旦超时、取消、超出内存、崩溃或通信异常，该进程**一律回收、不再复用**。
此外进程在执行任务数过多、常驻内存过大或空闲过久时也会退出。
"""
from __future__ import annotations

import atexit
import multiprocessing
import sys
import threading
import time
import tempfile
from dataclasses import dataclass, field

import psutil
from multiprocessing.connection import Connection

from pixelprobe_mcp.config import ServerConfig

MAX_TASKS_PER_WORKER = 200
IDLE_TIMEOUT_SECONDS = 300.0
GRACE_SECONDS = 2.0


# 工作进程只执行这三类固定操作；能力名在 CapabilityInput 中并由能力登记表校验
OPERATIONS = {
    "capability": "CapabilityInput",
    "list_artifacts": "BundleListInput",
    "read_artifact": "ArtifactReadInput",
}


class WorkerError(RuntimeError):
    """工作进程返回的已脱敏错误。"""


def execute_operation(config: ServerConfig, operation: str, payload: dict, cancellation=None):
    from pixelprobe_mcp import models
    from pixelprobe_mcp.service import PixelProbeService
    model = getattr(models, OPERATIONS[operation])
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    service = PixelProbeService(config, cancellation=cancellation)
    method = service.run_capability if operation == "capability" else getattr(service, operation)
    result = method(model.model_validate(payload))
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    return result


def _worker_loop(connection: Connection, config: ServerConfig, cancel_event) -> None:
    """常驻工作进程主循环：逐个接收任务；收到 None、空闲超时或主进程断开时退出。"""
    try:
        while True:
            if not connection.poll(IDLE_TIMEOUT_SECONDS):
                return
            try:
                task = connection.recv()
            except (EOFError, OSError):
                return
            if task is None:
                return
            operation, payload, memory_limit, disk_limit, scratch = task
            _run_task(connection, config, operation, payload, cancel_event,
                      memory_limit, disk_limit, scratch)
    finally:
        connection.close()


def _run_task(connection: Connection, config: ServerConfig, operation: str, payload: dict,
              cancel_event, memory_limit: int, disk_limit: int, scratch: str) -> None:
    from pixelprobe.engine.execution import CancellationToken
    from pydantic import ValidationError
    from pixelprobe.domain.errors import DomainError
    from pixelprobe.models.errors import PixelProbeError
    from pixelprobe_mcp.config import MediaChangedError, PathAccessError
    from pixelprobe_mcp.service import McpInputError, McpResourceLimitError, validation_message
    from pixelprobe.utils.resource_budget import ResourceBudget, budget_scope
    from pixelprobe_mcp.resource_plan import plan_operation, CONTROL_BYTES
    tempfile.tempdir = scratch
    budget = None

    def usage() -> dict:
        assert budget is not None
        return {"peak_managed_disk_bytes": budget.peak_disk_bytes,
                "retained_managed_disk_bytes": sum(budget.sizes.values()),
                "bundle_verification_passes": budget.bundle_verification_passes,
                "bundle_verified_bytes": budget.bundle_verified_bytes}

    try:
        token = CancellationToken(cancel_event)
        # 预检本身也在同一内存/时限监控之内；不在协议主进程解析媒体。
        plan = plan_operation(config, operation, payload)
        planned_memory = max(plan.get("memory_bytes", memory_limit),
                             psutil.Process().memory_info().rss + 16 * 1024 * 1024)
        planned_disk = max(CONTROL_BYTES, plan.get("temporary_bytes", disk_limit))
        if planned_memory > memory_limit or planned_disk > disk_limit:
            raise McpResourceLimitError("实际执行计划超过任务预算；请缩小范围或提高总预算")
        plan.update(memory_bytes=planned_memory, temporary_bytes=planned_disk)
        connection.send(("plan", plan))
        budget = ResourceBudget(planned_memory, planned_disk - CONTROL_BYTES,
                                token.raise_if_cancelled)
        with budget_scope(budget):
            result = execute_operation(config, operation, payload, token)
        connection.send(("usage", usage()))
    except ValidationError as exc:
        message = f"参数无效：{validation_message(exc)}"
    except (DomainError, PixelProbeError) as exc:
        details = exc.to_dict()
        message = f"{details['code']}：{details['message']}"
        if details.get("hint"):
            message += f"；建议：{details['hint']}"
    except PathAccessError:
        message = "PATH_NOT_ALLOWED：请检查 PIXELPROBE_MCP_ROOTS 和输入路径"
    except MediaChangedError:
        message = "MEDIA_CHANGED_DURING_ANALYSIS：输入发生变化，结果已丢弃"
    except McpResourceLimitError as exc:
        message = f"MCP_RESOURCE_LIMIT：{exc}"
    except McpInputError as exc:
        message = f"参数无效：{exc}"
    except MemoryError:
        message = "MCP_RESOURCE_LIMIT：工作进程内存分配失败"
    except (ValueError, OSError, KeyError, RuntimeError):
        message = "MCP_OPERATION_FAILED：请检查参数、媒体完整性和资源限制"
    except Exception:
        message = "MCP_INTERNAL_ERROR：媒体工作进程执行失败"
    else:
        connection.send((True, result))
        return
    finally:
        tempfile.tempdir = None
    if budget is not None:
        connection.send(("usage", usage()))
    connection.send((False, message))


def run_operation_sync(
    config: ServerConfig, operation: str, payload: dict, *,
    timeout_seconds: float, cancel_event: threading.Event | None = None,
    memory_limit_bytes: int | None = None, temporary_limit_bytes: int | None = None,
    on_plan=None, on_usage=None,
):
    """在常驻工作进程中执行固定操作；超时、取消或异常后确认回收该进程。"""
    if operation not in OPERATIONS:
        raise ValueError("不支持的媒体操作")
    from pixelprobe_mcp.jobs import estimate_operation_resources
    default_memory, default_disk = estimate_operation_resources(operation, payload)
    memory_limit = min(memory_limit_bytes or default_memory, config.total_memory_bytes)
    disk_limit = min(default_disk if temporary_limit_bytes is None else temporary_limit_bytes,
                     config.total_temporary_bytes)
    pool = _pool_for(config)
    worker = pool.acquire(str(payload.get("media_path", "")))
    scratch = tempfile.TemporaryDirectory(prefix="pixelprobe-worker-")
    started_at = time.monotonic()
    usage = {"peak_worker_rss_bytes": 0, "memory_sampling_interval_ms": 20}
    # finished：工作进程按协议报告了本任务的最终结果（成功或已脱敏的失败），可放回空闲池
    finished = False
    try:
        worker.cancel_event.clear()
        worker.connection.send((operation, payload, memory_limit, disk_limit, scratch.name))
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise WorkerError("MCP_CANCELLED：任务已取消")
            if time.monotonic() - started_at >= timeout_seconds:
                raise TimeoutError
            try:
                memory_info = worker.monitored.memory_info()
                rss = max(memory_info.rss, getattr(memory_info, "peak_wset", 0))
                usage["peak_worker_rss_bytes"] = max(usage["peak_worker_rss_bytes"], rss)
            except psutil.NoSuchProcess:
                rss = 0
            except psutil.Error as exc:
                raise WorkerError("MCP_RESOURCE_MONITOR_FAILED：无法监测工作进程内存") from exc
            if rss > memory_limit:
                raise WorkerError("MCP_RESOURCE_LIMIT：工作进程实际内存超过预算，已停止执行")
            if worker.connection.poll(0.02):
                try:
                    success, result = worker.connection.recv()
                except (EOFError, OSError) as exc:
                    raise WorkerError("MCP_WORKER_CRASHED：媒体工作进程异常退出") from exc
                if success == "plan":
                    memory_limit = result["memory_bytes"]
                    if on_plan is not None:
                        on_plan(result)
                    continue
                if success == "usage":
                    usage.update(result)
                    continue
                break
            if not worker.process.is_alive():
                raise WorkerError("MCP_WORKER_CRASHED：媒体工作进程异常退出")
        finished = True
        if not success:
            raise WorkerError(result)
        return result
    finally:
        if finished:
            pool.release(worker)
        else:
            pool.retire(worker, busy=True)
        scratch.cleanup()
        if on_usage is not None:
            on_usage(usage)


@dataclass(eq=False)
class _PooledWorker:
    process: multiprocessing.Process
    connection: Connection
    cancel_event: object
    monitored: psutil.Process
    tasks_done: int = 0
    last_media: str = ""


@dataclass(eq=False)
class WorkerPool:
    """同一 ServerConfig 共用的常驻工作进程池；线程安全。"""

    config: ServerConfig
    _idle: list[_PooledWorker] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    spawned: int = 0

    def acquire(self, media_key: str) -> _PooledWorker:
        with self._lock:
            alive = [worker for worker in self._idle if worker.process.is_alive()]
            stale = [worker for worker in self._idle if worker not in alive]
            self._idle = alive
            chosen = next((w for w in alive if media_key and w.last_media == media_key), None)
            if chosen is None and alive:
                chosen = alive[-1]
            if chosen is not None:
                self._idle.remove(chosen)
        for worker in stale:
            self._close(worker)
        if chosen is None:
            chosen = self._spawn()
        chosen.last_media = media_key
        return chosen

    def release(self, worker: _PooledWorker) -> None:
        worker.tasks_done += 1
        try:
            rss = worker.monitored.memory_info().rss
        except psutil.Error:
            rss = None
        keep = (
            worker.process.is_alive()
            and rss is not None
            and worker.tasks_done < MAX_TASKS_PER_WORKER
            and rss <= self.config.total_memory_bytes // 2
        )
        with self._lock:
            if keep and len(self._idle) < max(1, self.config.workers):
                self._idle.append(worker)
                return
        self.retire(worker, busy=False)

    def retire(self, worker: _PooledWorker, *, busy: bool) -> None:
        """回收进程：忙碌中先协作取消并给宽限期，再要求退出，最后强制结束。"""
        if busy:
            worker.cancel_event.set()
        try:
            if worker.process.is_alive():
                try:
                    worker.connection.send(None)
                except (OSError, ValueError):
                    pass
                worker.process.join(GRACE_SECONDS)
            if worker.process.is_alive():
                worker.process.terminate()
                worker.process.join(GRACE_SECONDS)
            if worker.process.is_alive():
                worker.process.kill()
            worker.process.join()
        finally:
            self._close(worker)

    def shutdown(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for worker in idle:
            self.retire(worker, busy=False)

    @property
    def idle_count(self) -> int:
        with self._lock:
            return len(self._idle)

    def _spawn(self) -> _PooledWorker:
        context = multiprocessing.get_context("spawn")
        parent_end, child_end = context.Pipe(duplex=True)
        process_type = context.Process
        if sys.platform == "win32":
            from pixelprobe_mcp.windows_spawn import HiddenSpawnProcess
            process_type = HiddenSpawnProcess
        cancel_event = context.Event()
        process = process_type(
            target=_worker_loop, args=(child_end, self.config, cancel_event), daemon=True,
        )
        try:
            process.start()
        finally:
            child_end.close()
        self.spawned += 1
        return _PooledWorker(
            process=process, connection=parent_end, cancel_event=cancel_event,
            monitored=psutil.Process(process.pid),
        )

    @staticmethod
    def _close(worker: _PooledWorker) -> None:
        try:
            worker.connection.close()
        except OSError:
            pass
        try:
            if not worker.process.is_alive():
                worker.process.join(0)
                worker.process.close()
        except ValueError:
            pass


_POOLS: dict[ServerConfig, WorkerPool] = {}
_POOLS_LOCK = threading.Lock()


def _pool_for(config: ServerConfig) -> WorkerPool:
    with _POOLS_LOCK:
        pool = _POOLS.get(config)
        if pool is None:
            pool = _POOLS[config] = WorkerPool(config)
        return pool


def shutdown_worker_pools() -> None:
    """回收全部空闲工作进程；正在执行的任务由各自调用方在结束时回收。"""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    for pool in pools:
        pool.shutdown()


atexit.register(shutdown_worker_pools)
