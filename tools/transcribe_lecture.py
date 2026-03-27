"""Backward-compatible lecture transcription entrypoint.

This keeps older operator commands working after the module moved to
``tools.services.transcribe_lecture``.
"""

from __future__ import annotations

import runpy

from tools.services.transcribe_lecture import transcribe_and_index

__all__ = ["transcribe_and_index"]


if __name__ == "__main__":
    runpy.run_module("tools.services.transcribe_lecture", run_name="__main__")
