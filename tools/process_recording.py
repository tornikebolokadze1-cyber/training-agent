"""Backward-compatible manual recording entrypoint.

This keeps older operator commands working after the module moved to
``tools.app.process_recording``.
"""

from __future__ import annotations

from tools.app.process_recording import main, process

__all__ = ["main", "process"]


if __name__ == "__main__":
    main()
