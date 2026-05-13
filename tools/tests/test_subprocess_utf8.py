"""Regression test: every subprocess call using text-mode output must declare encoding="utf-8".

Without explicit encoding=, Python falls back to the system locale codec (e.g. cp1252 on
Windows or ASCII on Railway's POSIX locale), which crashes with UnicodeDecodeError when
ffprobe/ffmpeg emits a Georgian filename in stderr/stdout.

This test walks every .py file in tools/integrations/ and tools/app/ using the AST module,
finds every subprocess.run() and subprocess.Popen() call that enables text-mode output, and
asserts that encoding="utf-8" is also present.

Run with:
    python -m pytest tools/tests/test_subprocess_utf8.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple


class UnsafeCall(NamedTuple):
    file: Path
    line: int
    source_snippet: str


_TEXT_MODE_KWARGS = {"text", "universal_newlines", "capture_output"}
_REQUIRED_ENCODING_KWARG = "encoding"


def _extract_kwarg_names(call_node: ast.Call) -> set[str]:
    """Return the set of keyword argument names present in an AST Call node."""
    return {kw.arg for kw in call_node.keywords if kw.arg is not None}


def _has_text_mode(kwarg_names: set[str]) -> bool:
    """Return True if any text-mode keyword is present."""
    return bool(kwarg_names & _TEXT_MODE_KWARGS)


def _has_encoding(kwarg_names: set[str]) -> bool:
    """Return True if encoding= is explicitly present."""
    return _REQUIRED_ENCODING_KWARG in kwarg_names


def _is_subprocess_call(call_node: ast.Call) -> bool:
    """Return True if the Call node looks like subprocess.run or subprocess.Popen."""
    func = call_node.func
    # subprocess.run(...) or subprocess.Popen(...)
    if isinstance(func, ast.Attribute):
        if func.attr in ("run", "Popen"):
            if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
                return True
    # Also catches: from subprocess import run; run(...)
    if isinstance(func, ast.Name) and func.id in ("run", "Popen"):
        return True
    return False


def find_unsafe_subprocess_calls(root: Path) -> list[UnsafeCall]:
    """Return UnsafeCall entries for every subprocess call that uses text-mode output
    without an explicit encoding= keyword argument.

    Args:
        root: Directory to search recursively for *.py files.

    Returns:
        List of UnsafeCall named tuples (file, line, source_snippet).
    """
    unsafe: list[UnsafeCall] = []

    for py_file in sorted(root.rglob("*.py")):
        # Skip test files — they patch subprocess.run, not call it for real
        if py_file.parent.name == "tests" or "test_" in py_file.name:
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        source_lines = source.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_subprocess_call(node):
                continue

            kwarg_names = _extract_kwarg_names(node)
            if not _has_text_mode(kwarg_names):
                # Binary mode — no encoding issue
                continue
            if _has_encoding(kwarg_names):
                # Correctly annotated
                continue

            # Get the source line (1-indexed → 0-indexed)
            line_idx = node.lineno - 1
            snippet = source_lines[line_idx].strip() if line_idx < len(source_lines) else "<unknown>"
            unsafe.append(UnsafeCall(file=py_file, line=node.lineno, source_snippet=snippet))

    return unsafe


def test_no_subprocess_call_without_explicit_encoding() -> None:
    """All subprocess.run/Popen calls that use text-mode output must declare encoding='utf-8'.

    Failure here means a new call site was added without the encoding kwarg — a latent
    UnicodeDecodeError landmine on Railway's POSIX locale when ffmpeg/ffprobe emits
    Georgian filenames in stderr.
    """
    tools_root = Path(__file__).parent.parent  # tools/

    integrations_issues = find_unsafe_subprocess_calls(tools_root / "integrations")
    app_issues = find_unsafe_subprocess_calls(tools_root / "app")
    all_issues = integrations_issues + app_issues

    if all_issues:
        lines = ["Subprocess call(s) use text-mode without encoding='utf-8':"]
        for issue in all_issues:
            rel = issue.file.relative_to(tools_root.parent)
            lines.append(f"  {rel}:{issue.line}  →  {issue.source_snippet}")
        lines.append("")
        lines.append(
            "Fix: add encoding='utf-8', errors='replace' to each call."
        )
        raise AssertionError("\n".join(lines))
