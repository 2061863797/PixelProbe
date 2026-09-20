"""媒体操作的 spawn 隔离执行；不接受任意可调用对象。"""
from __future__ import annotations

import multiprocessing
import sys
import threading
import time
import tempfile

import psutil
from multiprocessing.connection import Connection

import anyio

from pixelprobe_mcp.config import ServerConfig


READ_OPERATIONS = {
    "inspect_media": "InspectInput", "get_frame": "FrameInput",
    "read_pixels": "PixelInput", "analyze_region": "RegionInput",
    "find_changes": "ChangesInput", "list_artifacts": "BundleListInput",
    "read_artifact": "ArtifactReadInput",
}
OPERATIONS = {
    **READ_OPERATIONS,
    "generate_representation": "GenerateInput",
    "evidence": "EvidenceInput",
    "workflow": "WorkflowInput",
}


class WorkerError(RuntimeError):
    """工作进程返回的已脱敏错误。"""


def execute_operation(config: ServerConfig, operation: str, payload: dict, cancellation=None):
    from pixelprobe_mcp import models
    from pixelprobe_mcp.service import PixelProbeService
    model = getattr(models, OPERATIONS[operation])
    options = {}
    if operation == "generate_representation":
        payload = dict(payload)
        options["checkpoint_id"] = payload.pop("_checkpoint_id", None)
        options["resume"] = payload.pop("_resume", False)
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    result = getattr(PixelProbeService(config, cancellation=cancellation, **options), operation)(
        model.model_validate(payload),
    )
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    return result


def _worker(connection: Connection, config: ServerConfig, operation: str, payload: dict,
            cancel_event, memory_limit: int, disk_limit: int, scratch: str) -> None:
    from pixelprobe.engine.execution import CancellationToken
    from pydantic import ValidationError
    from pixelprobe.domain.errors import DomainError
    from pixelprobe.models.errors import PixelProbeError
    from pixelprobe_mcp.config import MediaChangedError, PathAccessError
    from pixelprobe_mcp.service import McpResourceLimitError, validation_message
    from pixelprobe.utils.resource_budget import ResourceBudget, budget_scope
    from pixelprobe_mcp.resource_plan import plan_operation, CONTROL_BYTES
    tempfile.tempdir = scratch
    budget = None
    try:
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
            connection.send(("usage", {"peak_managed_disk_bytes": budget.peak_disk_bytes,
                                       "retained_managed_disk_bytes": sum(budget.sizes.values()),
                                       "bundle_verification_passes": budget.bundle_verification_passes,
                                       "bundle_verified_bytes": budget.bundle_verified_bytes}))
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
        except MemoryError:
            message = "MCP_RESOURCE_LIMIT：工作进程内存分配失败"
        except (ValueError, OSError, KeyError, RuntimeError):
            message = "MCP_OPERATION_FAILED：请检查参数、媒体完整性和资源限制"
        except Exception:
            message = "MCP_INTERNAL_ERROR：媒体工作进程执行失败"
        else:
            connection.send((True, result))
            return
        if budget is not None:
            connection.send(("usage", {"peak_managed_disk_bytes": budget.peak_disk_bytes,
                                       "retained_managed_disk_bytes": sum(budget.sizes.values()),
                                       "bundle_verification_passes": budget.bundle_verification_passes,
                                       "bundle_verified_bytes": budget.bundle_verified_bytes}))
        connection.send((False, message))
    finally:
        connection.close()


def run_operation_sync(
    config: ServerConfig, operation: str, payload: dict, *,
    timeout_seconds: float, cancel_event: threading.Event | None = None,
    memory_limit_bytes: int | None = None, temporary_limit_bytes: int | None = None,
    on_plan=None, on_usage=None,
):
    """在 spawn 子进程执行固定操作，并在超时/取消后确认回收。"""
    if operation not in OPERATIONS:
        raise ValueError("不支持的媒体操作")
    from pixelprobe_mcp.jobs import estimate_operation_resources
    default_memory, default_disk = estimate_operation_resources(operation, payload)
    memory_limit = min(memory_limit_bytes or default_memory, config.total_memory_bytes)
    disk_limit = min(default_disk if temporary_limit_bytes is None else temporary_limit_bytes,
                     config.total_temporary_bytes)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process_type = context.Process
    if sys.platform == "win32":
        from pixelprobe_mcp.windows_spawn import HiddenSpawnProcess
        process_type = HiddenSpawnProcess
    cooperative_cancel = context.Event()
    scratch = tempfile.TemporaryDirectory(prefix="pixelprobe-worker-")
    process = process_type(target=_worker, args=(
        sender, config, operation, payload, cooperative_cancel,
        memory_limit, disk_limit, scratch.name,
    ))
    started = False
    started_at = time.monotonic()
    usage = {"peak_worker_rss_bytes": 0, "memory_sampling_interval_ms": 20}
    completed = False
    try:
        process.start()
        started = True
        sender.close()
        monitored = psutil.Process(process.pid)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cooperative_cancel.set()
                raise WorkerError("MCP_CANCELLED：任务已取消")
            if time.monotonic() - started_at >= timeout_seconds:
                cooperative_cancel.set()
                raise TimeoutError
            try:
                memory_info = monitored.memory_info()
                rss = max(memory_info.rss, getattr(memory_info, "peak_wset", 0))
                usage["peak_worker_rss_bytes"] = max(usage["peak_worker_rss_bytes"], rss)
            except psutil.NoSuchProcess:
                rss = 0
            except psutil.Error as exc:
                cooperative_cancel.set()
                raise WorkerError("MCP_RESOURCE_MONITOR_FAILED：无法监测工作进程内存") from exc
            if rss > memory_limit:
                cooperative_cancel.set()
                raise WorkerError("MCP_RESOURCE_LIMIT：工作进程实际内存超过预算，已停止执行")
            if receiver.poll(0.02):
                try:
                    success, result = receiver.recv()
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
            if not process.is_alive():
                raise WorkerError("MCP_WORKER_CRASHED：媒体工作进程异常退出")
        if not success:
            raise WorkerError(result)
        completed = True
        return result
    finally:
        if started and not completed:
            cooperative_cancel.set()
        if started:
            if process.is_alive():
                process.join(2.0)
            if process.is_alive():
                process.terminate()
                process.join(2.0)
            if process.is_alive():
                process.kill()
                process.join()
            else:
                process.join()
            process.close()
        sender.close()
        receiver.close()
        scratch.cleanup()
        if on_usage is not None:
            on_usage(usage)


async def run_operation(
    config: ServerConfig, operation: str, payload: dict, *, timeout_seconds: float,
    cancel_event: threading.Event | None = None,
):
    return await anyio.to_thread.run_sync(
        lambda: run_operation_sync(
            config, operation, payload, timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        ), abandon_on_cancel=True,
    )


async def run_read(config: ServerConfig, operation: str, payload: dict):
    """兼容既有调用；时限由外层取消域控制。"""
    return await run_operation(
        config, operation, payload, timeout_seconds=24 * 60 * 60,
    )
