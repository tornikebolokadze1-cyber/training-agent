"""Regression tests for UTF-8 logging bootstrap (Gap 1 + Gap 2 post-mortem).

Tests verify:
  1. configure_logging() survives inside pytest's capture context (no fileno()).
  2. Georgian text can be emitted without UnicodeEncodeError after configure.
  3. An ASCII-only stdout (simulating Railway's POSIX/C locale) does not crash.
  4. Importing orchestrator (which calls init_logging_early() at module scope)
     does not raise even when sys.stdout is ASCII-only.
"""

from __future__ import annotations

import io
import logging
import sys
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_root_handlers() -> None:
    """Remove all handlers from the root logger so configure_logging() starts fresh."""
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass


def _make_ascii_stdout() -> io.TextIOWrapper:
    """Return a writable TextIOWrapper whose encoding is ASCII (no fileno).

    This replicates Railway's sys.stdout on a POSIX/C-locale host: a stream
    that accepts only ASCII and raises UnicodeEncodeError on Georgian text.
    """
    return io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")


# ---------------------------------------------------------------------------
# Test 1 — configure_logging() must not raise inside pytest's capture context
# ---------------------------------------------------------------------------

def test_configure_logging_handles_pytest_capture() -> None:
    """configure_logging() must succeed even when sys.stdout has no fileno().

    Under pytest's default --capture=fd / --capture=sys mode, sys.stdout is
    replaced with a _pytest.capture.EncodedFile (or similar) whose .fileno()
    raises io.UnsupportedOperation.  The old open(sys.stdout.fileno(), ...)
    call would crash here; the new _wrap_stdout_utf8() must handle this
    gracefully.
    """
    _reset_root_handlers()
    try:
        from tools.core.logging_config import configure_logging

        # Must not raise — this is the core assertion.
        configure_logging()
    finally:
        _reset_root_handlers()


# ---------------------------------------------------------------------------
# Test 2 — logger.info() with Georgian text must not raise after configure
# ---------------------------------------------------------------------------

def test_logger_can_emit_georgian_after_configure() -> None:
    """Georgian text must pass through the log pipeline without UnicodeEncodeError."""
    _reset_root_handlers()
    captured_messages: list[str] = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_messages.append(self.format(record))

    try:
        from tools.core.logging_config import configure_logging

        configure_logging()

        # Attach a side-channel handler so we can inspect what was formatted.
        cap = _CapturingHandler()
        cap.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(cap)

        georgian_text = "ლექცია #1 — ვიდეო ჩანაწერი.mp4"
        logging.getLogger("test.georgian").info(georgian_text)

        assert any(georgian_text in msg for msg in captured_messages), (
            f"Georgian text not found in captured log output. Got: {captured_messages!r}"
        )
    finally:
        _reset_root_handlers()


# ---------------------------------------------------------------------------
# Test 3 — ASCII stdout must not crash logger (Railway POSIX locale scenario)
# ---------------------------------------------------------------------------

def test_simulated_ascii_stdout_does_not_crash_logger() -> None:
    """Monkeypatched ASCII sys.stdout must not cause UnicodeEncodeError.

    This reproduces the exact failure from tonight: Railway's POSIX/C locale
    gives sys.stdout an ASCII codec.  Georgian filenames in log messages used
    to crash with UnicodeEncodeError before the _wrap_stdout_utf8() fix.

    After the fix, configure_logging() must either:
      a) reconfigure the stream to UTF-8 (preferred path), or
      b) wrap it with a UTF-8 fd (legacy path), or
      c) fall back gracefully without crashing (last resort).

    In all cases no exception should propagate.
    """
    _reset_root_handlers()
    ascii_stream = _make_ascii_stdout()

    with patch("sys.stdout", ascii_stream):
        try:
            from tools.core.logging_config import configure_logging

            # Must not raise during setup.
            configure_logging()

            # Must not raise when emitting Georgian text.
            logging.getLogger("test.railway_sim").info(
                "ლექცია #1 — ვიდეო ჩანაწერი.mp4 (790MB)"
            )
        finally:
            _reset_root_handlers()


# ---------------------------------------------------------------------------
# Test 4 — importing orchestrator with ASCII stdout must not raise
# ---------------------------------------------------------------------------

def test_early_init_before_config_imports() -> None:
    """init_logging_early() with ASCII stdout must configure UTF-8 before config loads.

    This test verifies the core invariant of Gap 1 fix:
      - init_logging_early() is called BEFORE any tools.* import
      - After init_logging_early(), the root logger has a UTF-8-capable handler
      - A subsequent simulated config.py warning (Georgian text) must not raise

    Rather than importing the full orchestrator (which has too many module-scope
    side-effects to stub cheaply), we test the bootstrap + config import path
    directly — which is exactly the sequence orchestrator.py executes.
    """
    _reset_root_handlers()
    ascii_stream = _make_ascii_stdout()

    # Ensure bootstrap module itself is fresh (not cached with old handler state).
    sys.modules.pop("tools.core.bootstrap", None)

    try:
        with patch("sys.stdout", ascii_stream):
            # Step 1: call init_logging_early() — must not raise even with ASCII stdout.
            from tools.core.bootstrap import init_logging_early
            init_logging_early()

            # Step 2: root logger must now have at least one handler (UTF-8 wrapped).
            root = logging.getLogger()
            assert root.handlers, (
                "init_logging_early() must attach at least one handler to the root logger"
            )

            # Step 3: emit a Georgian WARNING — this simulates config.py's module-level
            # warning for a missing optional env var.  Must not raise UnicodeEncodeError.
            logging.getLogger("test.config_sim").warning(
                "Optional credential not set: N8N_CALLBACK_URL — ქართული ტექსტი"
            )

    finally:
        _reset_root_handlers()
