"""MCP 脱敏诊断编号与本地轮转日志。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import uuid

from pixelprobe_mcp.config import ServerConfig

_LOCK = threading.Lock()
_LOGGERS: dict[Path, logging.Logger] = {}


def record_diagnostic(
    config: ServerConfig,
    *,
    code: str,
    operation: str,
    exception_type: str | None = None,
) -> str:
    """仅记录操作名、错误码和异常类型，不记录媒体内容或完整路径。"""
    diagnostic_id = "pp-" + uuid.uuid4().hex[:12]
    try:
        root = config.prepare_artifact_root() / ".logs"
        root.mkdir(exist_ok=True)
        target = root / "pixelprobe-mcp.log"
        with _LOCK:
            logger = _LOGGERS.get(target)
            if logger is None:
                logger = logging.getLogger(f"pixelprobe_mcp.{hash(target)}")
                logger.setLevel(logging.INFO)
                logger.propagate = False
                handler = RotatingFileHandler(
                    target, maxBytes=2 * 1024 * 1024, backupCount=3,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s %(message)s"
                ))
                logger.addHandler(handler)
                _LOGGERS[target] = logger
        logger.error(
            "diagnostic_id=%s code=%s operation=%s exception_type=%s",
            diagnostic_id, code, operation, exception_type or "unknown",
        )
    except OSError:
        pass
    return diagnostic_id


def actionable_error(
    code: str,
    message: str,
    *,
    retryable: bool,
    action: str,
    diagnostic_id: str,
) -> str:
    return (
        f"{code}：{message}；可重试：{'是' if retryable else '否'}；"
        f"建议：{action}；诊断编号：{diagnostic_id}"
    )
