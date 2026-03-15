"""Structured logging configuration for the Training Agent system.

Provides two log formats:
  - Local: human-readable with timestamp, level, module, and message
  - Production (Railway): JSON lines for structured log ingestion

Usage:
    from tools.logging_config import configure_logging
    configure_logging()
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


class JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line.

    Structured fields: timestamp, level, logger, message, plus any extras.
    Used in production (Railway) for machine-parseable log ingestion.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        if hasattr(record, "correlation_id"):
            log_entry["correlation_id"] = record.correlation_id

        return json.dumps(log_entry, ensure_ascii=False)


# Human-readable format for local development
LOCAL_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
LOCAL_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    *,
    project_root: Path | None = None,
    force_json: bool = False,
) -> None:
    """Set up application-wide logging.

    Args:
        project_root: Root directory for log files (local only).
        force_json: Force JSON format even in local mode.
    """
    is_railway = bool(os.getenv("RAILWAY_ENVIRONMENT"))
    use_json = is_railway or force_json

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console handler — stdout
    console = logging.StreamHandler(sys.stdout)
    if use_json:
        console.setFormatter(JSONFormatter())
    else:
        console.setFormatter(
            logging.Formatter(LOCAL_FORMAT, datefmt=LOCAL_DATE_FORMAT)
        )
    root.addHandler(console)

    # File handler — local only (Railway captures stdout)
    if not is_railway and project_root is not None:
        log_dir = project_root / "logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "training_agent.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter(LOCAL_FORMAT, datefmt=LOCAL_DATE_FORMAT)
        )
        root.addHandler(file_handler)

    # Suppress chatty third-party loggers
    for noisy in (
        "apscheduler.scheduler",
        "apscheduler.executors",
        "httpx",
        "httpcore",
        "uvicorn.access",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
