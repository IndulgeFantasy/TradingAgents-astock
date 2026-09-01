"""Unified file logging setup for application entry points.

Library code only calls ``logging.getLogger(__name__)``; the actual
``RotatingFileHandler`` is attached here at the entry points (CLI / Web /
playwright_service).  Idempotent: repeated calls never stack handlers.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def _resolve_level(env_name: str, default: int = logging.INFO) -> int:
    raw = os.getenv(env_name, "").strip().upper()
    if not raw:
        return default
    level = getattr(logging, raw, None)
    return level if isinstance(level, int) else default


def setup_file_logging(
    log_dir: Path,
    log_name: str,
    level: int | None = None,
    env_level: str = "TA_LOG_LEVEL",
) -> Path:
    """Attach a rotating file handler to the root logger.

    Args:
        log_dir: directory for the log file (created if missing).
        log_name: file stem, e.g. ``"tradingagents_cli"`` -> ``<dir>/<stem>.log``.
        level: explicit level; overridden by the ``env_level`` variable when set.
        env_level: environment variable name to override the level (e.g.
            ``TA_LOG_LEVEL=DEBUG``).  ``TA_LOG_LEVEL`` is the default; pass a
            different name (e.g. ``AKD_LOG_LEVEL``) for the standalone service.

    Returns:
        The resolved log file path.
    """
    if level is None:
        level = _resolve_level(env_level)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{log_name}.log"

    root = logging.getLogger()
    # Idempotency: skip if a file handler for the same path already exists.
    if not any(
        isinstance(h, RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")).resolve() == log_path.resolve()
        for h in root.handlers
    ):
        handler = RotatingFileHandler(
            log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
    # 终端输出 (stderr): 与文件同级双输出, 便于排查。
    # 注意: FileHandler 是 StreamHandler 的子类, 必须排除它,
    # 且 stderr 与 rich(stdout) 分离, CLI 全屏表格不会被日志重绘打断。
    if not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(stream)
    root.setLevel(level)
    return log_path
