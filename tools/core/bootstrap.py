"""Early logging bootstrap for the Training Agent.

This module must be imported BEFORE any ``tools.*`` module that might emit
log records at import time (most critically ``tools.core.config``, which
calls ``logger.warning()`` for missing optional env vars at module scope).

If logging has not been configured yet, Python's ``lastResort`` handler
writes to the raw ``sys.stderr`` using ASCII encoding.  On Railway's
POSIX/C locale that causes ``UnicodeEncodeError`` for Georgian text —
the exact failure this bootstrap prevents.

Usage (first lines of any entrypoint, after stdlib imports):

    from tools.core.bootstrap import init_logging_early
    init_logging_early()

``init_logging_early()`` is idempotent: subsequent calls are no-ops once
the root logger already has handlers attached.
"""

from __future__ import annotations

import logging


def init_logging_early() -> None:
    """Configure the root logger with a UTF-8-safe stdout handler.

    Delegates to ``tools.core.logging_config.configure_logging`` with no
    arguments (no project_root → no file handler, no force_json).  The
    full configuration (project_root, JSON format detection) is applied
    later when the orchestrator calls ``configure_logging(project_root=...)``
    in ``start()``.

    Safe to call multiple times — exits immediately if the root logger
    already has handlers (i.e., ``configure_logging`` was already called).
    """
    root = logging.getLogger()
    if root.handlers:
        # Already configured — nothing to do.
        return

    # Import is deferred to here so that *this* module itself does not trigger
    # any module-level logging in logging_config (it has none, but belt +
    # suspenders).
    from tools.core.logging_config import configure_logging

    configure_logging()
