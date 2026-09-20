"""本地执行上下文、取消、超时与严格 checkpoint。"""

from __future__ import annotations

import base64
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from pixelprobe.engine.errors import (
    CheckpointIncompatibleError,
    ExecutionCancelledError,
    ExecutionTimeoutError,
)
from pixelprobe.engine.cache import LocalArrayCache
from pixelprobe.operators.base import ResourcePolicy
from pixelprobe.utils.resource_budget import ResourceBudget, current_budget


class CancellationToken:
    def __init__(self, event=None) -> None:
        self._event = event if event is not None else threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ExecutionCancelledError("执行已取消")


class CheckpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "0.1.0"
    plan_id: str
    request_sha256: str
    input_sha256: str
    operator_versions: dict[str, str]
    completed_chunks: tuple[tuple[int, ...], ...]
    state_base64: str


@dataclass(slots=True)
class LocalExecutionContext:
    resources: ResourcePolicy
    temporary_root: Path
    cache: LocalArrayCache | None = None
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    events: list[dict[str, object]] = field(default_factory=list)
    _started: float = field(default_factory=time.monotonic)
    _temporary_sizes: dict[Path, int] = field(default_factory=dict)
    budget: ResourceBudget = field(init=False)

    def __post_init__(self) -> None:
        self.temporary_root.mkdir(parents=True, exist_ok=True)
        self.budget = current_budget() or ResourceBudget(
            self.resources.max_memory_bytes, self.resources.max_temporary_bytes,
            self.ensure_active,
        )
        self._temporary_sizes = self.budget.sizes

    def ensure_active(self) -> None:
        self.cancellation.raise_if_cancelled()
        timeout = self.resources.timeout_seconds
        if timeout is not None and time.monotonic() - self._started > timeout:
            raise ExecutionTimeoutError(f"执行超过 {timeout} 秒")

    def reserve_temporary(self, path: Path, size: int) -> None:
        """在写入前合计本次执行的受管文件，包括尚未刷盘的预留。"""
        self.budget.reserve(path, size)

    def release_temporary(self, path: Path) -> None:
        """仅在文件已移除后释放预算；失败时保留预留。"""
        path = Path(path).resolve()
        if path.exists():
            raise ValueError("临时文件尚未移除，不能释放预算")
        self.budget.removed(path)

    def report_progress(
        self, node_id: str, completed: int, total: int | None,
    ) -> None:
        self.ensure_active()
        self.events.append({
            "type": "progress", "node_id": node_id,
            "completed": completed, "total": total,
        })

    def checkpoint(self, name: str, record: CheckpointRecord) -> Path:
        return self.checkpoint_to(
            self.temporary_root / f"{name}.checkpoint.json", record,
        )

    def checkpoint_to(self, target: Path, record: CheckpointRecord) -> Path:
        self.ensure_active()
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".tmp-{uuid.uuid4().hex}")
        data = record.model_dump_json(indent=2).encode("utf-8") + b"\n"
        try:
            if target.exists():
                self.budget.reserve(target, target.stat().st_size)
            self.budget.reserve(temporary, len(data))
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self.budget.moved(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
            self.budget.removed(temporary)
        return target


    def load_checkpoint(
        self,
        path: Path,
        *,
        plan_id: str,
        request_sha256: str,
        input_sha256: str,
        operator_versions: dict[str, str],
    ) -> tuple[CheckpointRecord, bytes]:
        try:
            record = CheckpointRecord.model_validate_json(Path(path).read_bytes())
            state = base64.b64decode(record.state_base64, validate=True)
        except Exception as exc:
            raise CheckpointIncompatibleError("checkpoint 无法解析") from exc
        expected = (plan_id, request_sha256, input_sha256, operator_versions)
        actual = (
            record.plan_id, record.request_sha256,
            record.input_sha256, record.operator_versions,
        )
        if actual != expected:
            raise CheckpointIncompatibleError("checkpoint 与当前执行语义不一致")
        return record, state


def encoded_state(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
