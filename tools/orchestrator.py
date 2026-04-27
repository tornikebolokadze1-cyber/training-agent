"""Backward-compatible orchestrator entrypoint.

This keeps older launchd plists and operator commands working after the
package split to ``tools.app.orchestrator``.
"""

from __future__ import annotations

from tools.app.orchestrator import start

__all__ = ["start"]


if __name__ == "__main__":
    start()
