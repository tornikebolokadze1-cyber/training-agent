"""
redact_pii.py — CLI utility to redact student PII from markdown/text files.

Usage:
    python -m scripts.redact_pii <input_path> <output_path>
    python -m scripts.redact_pii --stats <input_path>

Redacts:
  - Phone numbers (995-prefix and raw 9+ digit sequences)
  - Named students (Georgian/Latin, whole-word matching)
  - Email addresses
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Student name → code mapping
# Order matters: longer / more specific patterns first to avoid partial matches.
# ---------------------------------------------------------------------------
STUDENT_MAP: list[tuple[str, str]] = [
    # G1
    ("Shorena", "student_G1_A"),
    ("ნინო ბეგლარაშვილი", "student_G1_B"),
    ("Lika Lejava", "student_G1_C"),
    ("Koba", "student_G1_D"),
    ("Keti", "student_G1_E"),
    ("Keso", "student_G1_F"),
    ("Giorgi Iakobashvili", "student_G1_G"),
    ("Lasha", "student_G1_H"),
    ("Maka", "student_G1_I"),
    # G1 — standalone first names (after full names above)
    ("Lika", "student_G1_C"),
    ("ნინო", "student_G1_B"),
    # G2 — full names first
    ("Misho Laliashvili", "student_G2_A"),
    ("Nikoloz Maisuradze", "student_G2_B"),
    ("beqa chkhubadze", "student_G2_C"),
    ("Tornike Motsonelidze", "student_G2_D"),  # different Tornike — student
    ("Mariam Lekveishvili", "student_G2_F"),
    ("ვიშ მოტორს", "student_G2_G"),
    ("Neli Kharbedia", "student_G2_H"),
    ("Tamar Parunashvili", "student_G2_K"),
    ("მადონა", "student_G2_L"),
    # G2 — standalone first names (after full names)
    ("Misho", "student_G2_A"),
    ("Nikoloz", "student_G2_B"),
    ("beqa", "student_G2_C"),
    ("Mariam", "student_G2_F"),
    ("Neli", "student_G2_H"),
    ("Tamar", "student_G2_K"),
    # Giorgi after Giorgi Iakobashvili (more specific first)
    ("Giorgi", "student_G2_E"),
    # Tato with emoji variants first, then bare
    ("Tato🎈🎈🎈", "student_G2_I"),
    ("Tato", "student_G2_I"),
    ("TIKO", "student_G2_J"),
]

# ---------------------------------------------------------------------------
# Regex patterns (compiled once)
# ---------------------------------------------------------------------------

# Phone: 995 + 9 digits, or standalone 9+ digit runs that look like numbers
_RE_PHONE_995 = re.compile(r"\b995\d{9}\b")
# Bare 9+ digit numeric sequences not already caught by 995-prefix
_RE_PHONE_BARE = re.compile(r"(?<!\d)\d{9,}(?!\d)")
# Email
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _build_name_pattern(name: str) -> re.Pattern[str]:
    """
    Build a whole-word pattern for a student name.
    Handles Georgian Unicode (which lacks \\b word boundary support),
    Latin names with emojis, and mixed-script names.
    """
    escaped = re.escape(name)
    # Use lookahead/lookbehind for word boundary:
    # - Not preceded/followed by word characters OR Georgian letters
    return re.compile(
        r"(?<![\\wა-ჿႠ-჏])"
        + escaped
        + r"(?![\\wა-ჿႠ-჏])",
        re.UNICODE,
    )


# Pre-compile all name patterns
_NAME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_build_name_pattern(name), code) for name, code in STUDENT_MAP
]


# ---------------------------------------------------------------------------
# Core redaction logic
# ---------------------------------------------------------------------------

@dataclass
class RedactionStats:
    phones: int = 0
    emails: int = 0
    names: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.phones + self.emails + sum(self.names.values())

    def report(self) -> str:
        lines = [
            f"  Phones   : {self.phones}",
            f"  Emails   : {self.emails}",
            f"  Names    : {sum(self.names.values())}",
        ]
        for name, count in sorted(self.names.items(), key=lambda x: -x[1]):
            lines.append(f"    {name:<30} → {count}")
        lines.append(f"  TOTAL    : {self.total}")
        return "\n".join(lines)


def redact_text(text: str) -> tuple[str, RedactionStats]:
    stats = RedactionStats()

    # 1. 995-prefix phones
    def _replace_phone_995(m: re.Match[str]) -> str:
        stats.phones += 1
        return "[PHONE]"

    text = _RE_PHONE_995.sub(_replace_phone_995, text)

    # 2. Bare 9+ digit phone sequences
    def _replace_phone_bare(m: re.Match[str]) -> str:
        stats.phones += 1
        return "[PHONE]"

    text = _RE_PHONE_BARE.sub(_replace_phone_bare, text)

    # 3. Student names (longest/most-specific first per STUDENT_MAP order)
    for pattern, code in _NAME_PATTERNS:
        def _replace_name(m: re.Match[str], _code: str = code, _pat: re.Pattern[str] = pattern) -> str:
            original = m.group(0)
            stats.names[original] = stats.names.get(original, 0) + 1
            return _code

        text = pattern.sub(_replace_name, text)

    # 4. Emails
    def _replace_email(m: re.Match[str]) -> str:
        stats.emails += 1
        return "[EMAIL]"

    text = _RE_EMAIL.sub(_replace_email, text)

    return text, stats


def redact_file(input_path: Path, output_path: Path) -> RedactionStats:
    """Read input, redact PII, write sanitized output. Originals untouched."""
    raw = input_path.read_text(encoding="utf-8")
    sanitized, stats = redact_text(raw)
    output_path.write_text(sanitized, encoding="utf-8")
    return stats


def stats_only(input_path: Path) -> RedactionStats:
    """Run redaction in memory and return stats without writing any file."""
    raw = input_path.read_text(encoding="utf-8")
    _, stats = redact_text(raw)
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Redact student PII from markdown/text files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show redaction counts only — do not write output file.",
    )
    parser.add_argument("input", type=Path, help="Path to input file.")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Path to write sanitized output (required unless --stats).",
    )
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 1

    if args.stats:
        stats = stats_only(args.input)
        print(f"Redaction stats for: {args.input}")
        print(stats.report())
        return 0

    if args.output is None:
        print("ERROR: output path required when not using --stats.", file=sys.stderr)
        return 1

    if args.output == args.input:
        print("ERROR: output path must differ from input (originals stay intact).", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    stats = redact_file(args.input, args.output)

    print(f"Sanitized → {args.output}")
    print(stats.report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
