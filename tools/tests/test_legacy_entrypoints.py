"""Compatibility tests for pre-restructure module entrypoints.

These keep older operator commands and launchd plists working after the
`tools/ -> tools/app|services` package split.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    ("legacy_module", "expected_attr"),
    [
        ("tools.orchestrator", "start"),
        ("tools.transcribe_lecture", "transcribe_and_index"),
        ("tools.process_recording", "process"),
    ],
)
def test_legacy_entrypoint_modules_remain_importable(
    legacy_module: str,
    expected_attr: str,
) -> None:
    """Old module paths should keep exposing the public entrypoint API."""
    module = importlib.import_module(legacy_module)
    assert hasattr(module, expected_attr)
