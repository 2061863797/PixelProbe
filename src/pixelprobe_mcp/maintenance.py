"""受管任务存储维护；由 JobManager 持锁调用。"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from pixelprobe_mcp.config import _is_reparse_point

_ID = re.compile(r"[0-9a-f]{32}")
_TERMINAL = {"succeeded", "failed", "cancelled", "timeout", "interrupted"}
_LIMIT = 10000


def _plain(path: Path) -> bool:
    return not path.is_symlink() and not _is_reparse_point(path)


def _tree_size(root: Path, *, older_than: float | None = None) -> int:
    """拒绝包含链接的整棵目录树；限制扫描规模。"""
    pending, total, count = [root], 0, 0
    while pending:
        path = pending.pop()
        count += 1
        if count > _LIMIT or not _plain(path):
            raise ValueError("目录过大或包含链接")
        if older_than is not None and path.stat().st_mtime >= older_than:
            raise ValueError("目录尚未过期")
        if path.is_dir():
            for child in path.iterdir():
                if count + len(pending) >= _LIMIT:
                    raise ValueError("目录过大")
                pending.append(child)
        elif path.is_file():
            total += path.stat().st_size
        else:
            raise ValueError("不是普通文件")
    return total


def maintain(manager, *, dry_run: bool = True) -> dict:
    root = manager._ensure_store()
    report = {"dry_run": dry_run, "expired_jobs": 0, "checkpoint_directories": 0,
              "reclaimable_bytes": 0, "removed_bytes": 0, "removed_jobs": 0,
              "removed_checkpoints": 0, "skipped": 0, "blocked": False,
              "managed_bytes": 0, "orphan_checkpoints": 0,
              "unidentified_checkpoints": 0}
    # 含正在回收的进程时也阻止清理，不能仅检查任务终态。
    if manager._outstanding or manager._other_storage_owner():
        report["blocked"] = True
        return report
    records, expired = {}, set()
    uncertain = False
    cutoff = time.time() - manager.config.job_retention_days * 86400
    for index, path in enumerate(root.iterdir()):
        if index >= _LIMIT:
            report["blocked"] = True
            return report
        if path.suffix != ".json" or not _ID.fullmatch(path.stem):
            continue
        try:
            if not _plain(path):
                raise ValueError("记录是链接")
            record = json.loads(path.read_text("utf-8"))
            if not isinstance(record, dict) or record.get("job_id") != path.stem:
                raise ValueError("记录无效")
            status = record.get("status")
            from pixelprobe_mcp.worker import OPERATIONS
            if record.get("operation") not in OPERATIONS:
                raise ValueError("记录操作未知")
            if status in {"queued", "running"}:
                report["blocked"] = True
                return report
            if status not in _TERMINAL:
                raise ValueError("终态未知")
            finished_value = record.get("finished_at")
            if not isinstance(finished_value, str):
                raise ValueError("终态时间无效")
            finished = datetime.fromisoformat(finished_value.replace("Z", "+00:00"))
            if finished.tzinfo is None:
                raise ValueError("终态时间缺少时区")
            checkpoint = record.get("checkpoint_id")
            if checkpoint is not None and not _ID.fullmatch(checkpoint):
                raise ValueError("checkpoint ID 无效")
            records[path.stem] = record
            if finished.timestamp() < cutoff:
                expired.add(path.stem)
        except (OSError, ValueError, KeyError, TypeError):
            uncertain = True
            report["skipped"] += 1
    # 无法解析记录时不能确定缓存引用，不推测哪些缓存已经无主。
    protected = {record.get("checkpoint_id") for key, record in records.items() if key not in expired}
    checkpoint_ids = {records[key].get("checkpoint_id") for key in expired} - protected - {None}
    referenced = {record.get("checkpoint_id") for record in records.values()}
    checkpoint_root = root.parent / ".checkpoints"
    if checkpoint_root.exists() and _plain(checkpoint_root) and not uncertain:
        for index, path in enumerate(checkpoint_root.iterdir()):
            if index >= _LIMIT:
                report["blocked"] = True
                return report
            if path.name in referenced:
                continue
            try:
                if not _ID.fullmatch(path.name) or not _plain(path):
                    raise ValueError("目录归属未知")
                marker = path / ".pixelprobe-owned.json"
                if not _plain(marker) or json.loads(marker.read_text("utf-8")) != {
                    "owner": "pixelprobe", "kind": "checkpoint", "checkpoint_id": path.name,
                }:
                    raise ValueError("标记无效")
                _tree_size(path, older_than=cutoff)
                checkpoint_ids.add(path.name)
                report["orphan_checkpoints"] += 1
            except (OSError, ValueError):
                report["unidentified_checkpoints"] += 1
    checkpoints, unresolved = [], set()
    for checkpoint_id in checkpoint_ids:
        if uncertain:
            unresolved.add(checkpoint_id)
            report["skipped"] += 1
            continue
        try:
            path = manager.config.checkpoint_directory(checkpoint_id)
            size = _tree_size(path)
            checkpoints.append((checkpoint_id, path, size))
        except (OSError, ValueError):
            report["skipped"] += 1
            # 目录确实不存在时可仅清理任务；权限或链接异常时保留引用。
            candidate = root.parent / ".checkpoints" / checkpoint_id
            if candidate.exists() or candidate.is_symlink():
                unresolved.add(checkpoint_id)
    files = {}
    for job_id in expired:
        if records[job_id].get("checkpoint_id") in unresolved:
            continue
        candidates = [root / f"{job_id}{suffix}" for suffix in
                      (".json", ".request.json", ".result.json", ".png")]
        # 仅处理已知任务的原子写入残留，不按宽泛通配符删除用户文件。
        candidates.extend(path for path in root.glob(f"{job_id}*.tmp-*")
                          if re.fullmatch(job_id + r"(?:\.result|\.request)?\.tmp-[0-9a-f]{32}", path.name))
        try:
            existing = [(path, path.stat().st_size) for path in candidates
                        if path.exists() or path.is_symlink()]
            if any(not _plain(path) or not path.is_file() for path, _ in existing):
                raise ValueError("任务文件不是普通文件")
            files[job_id] = existing
        except (OSError, ValueError):
            report["skipped"] += 1
    # 未能安全删除任务记录时保留它所引用的 checkpoint。
    checkpoints = [(key, path, size) for key, path, size in checkpoints
                   if all(job_id in files for job_id in expired
                          if records[job_id].get("checkpoint_id") == key)]
    report["expired_jobs"] = len(files)
    report["checkpoint_directories"] = len(checkpoints)
    report["reclaimable_bytes"] = sum(size for values in files.values() for _, size in values)
    report["reclaimable_bytes"] += sum(size for _, _, size in checkpoints)
    # 占用查询也只扫描受管位置，未知/过大的树明确计入 skipped。
    for path in (root, root.parent / ".checkpoints"):
        if path.exists():
            try:
                report["managed_bytes"] += _tree_size(path)
            except (OSError, ValueError):
                report["skipped"] += 1
    if dry_run:
        return report
    failed_checkpoints = set()
    for checkpoint_id, path, size in checkpoints:
        try:
            # 删除前再次核对绝对位置、目录身份及链接边界。
            expected = manager.config.checkpoint_directory(checkpoint_id).resolve(strict=True)
            if path.resolve(strict=True) != expected:
                raise ValueError("checkpoint 位置变化")
            _tree_size(path)
            shutil.rmtree(path)
            report["removed_checkpoints"] += 1
            report["removed_bytes"] += size
        except (OSError, ValueError):
            failed_checkpoints.add(checkpoint_id)
            report["skipped"] += 1
    for job_id, values in files.items():
        if records[job_id].get("checkpoint_id") in failed_checkpoints:
            continue
        try:
            # 最后删除索引；中途失败后仍可重试。
            for path, size in sorted(values, key=lambda item: item[0].name == f"{job_id}.json"):
                if not _plain(path) or path.resolve().parent != root.resolve():
                    raise ValueError("任务文件位置变化")
                path.unlink()
                report["removed_bytes"] += size
            manager._jobs.pop(job_id, None)
            report["removed_jobs"] += 1
        except (OSError, ValueError):
            report["skipped"] += 1
    return report
