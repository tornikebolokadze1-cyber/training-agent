# mypy Baseline — 2026-05-14

Captured during ralph US-024 (Tier C7 mypy triage) on branch
`fix/multi-cohort-cleanup-pr39` at HEAD `6cc8f85`.

Command run:

```bash
python -m mypy \
  tools/app/server.py \
  tools/app/orchestrator.py \
  tools/app/scheduler.py \
  tools/app/admin_routes.py \
  tools/integrations/zoom_manager.py \
  tools/integrations/obsidian_sync.py \
  tools/integrations/whatsapp_sender.py \
  tools/services/analytics.py \
  tools/services/drive_audit.py \
  tools/core/pipeline_state.py \
  tools/core/cost_tracker.py \
  --ignore-missing-imports
```

**Result: 85 errors in 11 source files (checked).**

> mypy version: 1.20.0 (compiled: yes)

This snapshot is **purely informational** — Wave 1-3 of the 2026-05-13
ralph session was deliberately scoped to runtime fixes and tests. No
mypy errors were introduced by that session (Wave 1-3 touched lines
were verified individually). Future sessions should track this number
and ensure it trends down.

## Per-File Breakdown

| File | Errors |
|---|---:|
| `tools/integrations/gemini_analyzer.py` | 28 |
| `tools/integrations/knowledge_indexer.py` | 14 |
| `tools/app/admin_routes.py` | 9 |
| `tools/app/orchestrator.py` | 8 |
| `tools/services/whatsapp_assistant.py` | 6 |
| `tools/services/analytics.py` | 6 |
| `tools/integrations/obsidian_sync.py` | 6 |
| `tools/app/openclaw_bridge.py` | 3 |
| `tools/services/drive_audit.py` | 2 |
| `tools/app/server.py` | 2 |
| `tools/core/retry.py` | 1 |

(Note: `whatsapp_assistant`, `openclaw_bridge`, `retry` showed up in
the cross-import chain even though they were not in the explicit file
list — mypy follows imports.)

## Errors by Kind

| Error kind | Count |
|---|---:|
| `arg-type` | 39 |
| `attr-defined` | 21 |
| `has-type` | 9 |
| `union-attr` | 8 |
| `assignment` | 5 |
| `index` | 2 |
| `call-arg` | 1 |

## What the Categories Mean

- **arg-type** (39): a function is being called with an argument whose
  inferred type does not match the declared parameter type. Most are in
  `gemini_analyzer.py` where the SDK's `types.GenerateContentConfig`
  shape leaks into our wrappers without explicit casts.
- **attr-defined** (21): code accesses an attribute mypy cannot prove
  exists. Two flavours:
  1. SDK / third-party Mock objects (`google.genai` types) — needs `# type: ignore[attr-defined]` or a Protocol.
  2. Defensive `getattr(...)` chains where the dynamic key is unknown
     at typecheck time.
- **has-type** (9): forward reference to a name whose type mypy
  cannot infer yet. Usually fixed by adding an explicit annotation
  earlier in the function.
- **union-attr** (8): code calls `.foo()` on something typed
  `T | None`. Needs an `if x is not None:` narrow first.
- **assignment** (5): an assignment whose RHS type does not match the
  declared LHS type. Often a refactor leftover.
- **index** (2), **call-arg** (1): minor — wrong arg name or wrong
  index type.

## Recommended Cleanup Order (Future Work)

1. **gemini_analyzer.py** — single file accounts for 33% of errors.
   Adding a `genai.GenerateContentConfig` Protocol stub + targeted
   `# type: ignore[arg-type]` for known-good SDK calls would knock
   most of these out.
2. **knowledge_indexer.py** — 14 errors, similar Pinecone SDK shape
   issues. Could be fixed alongside #1.
3. **admin_routes.py** (9) and **orchestrator.py** (8) — internal
   types only (no SDK). These are real type holes worth fixing
   properly, not silencing.
4. The rest are 6 or fewer per file — quick wins.

## What This Baseline Does NOT Track

- Modules outside the 11-file explicit list (mypy may follow imports
  and produce additional errors not enumerated here).
- The 39 `arg-type` errors include some that may be **false positives**
  from third-party Mock/MagicMock returns. A first cleanup pass should
  triage those before bulk-suppressing.
- mypy `--strict` flags are NOT enabled. Re-running with `--strict`
  would produce significantly more errors and is out of scope for the
  baseline.

## Configuration Note

The project has no `mypy.ini` or `[tool.mypy]` section in
`pyproject.toml` as of this snapshot. A reasonable starting config
would be:

```toml
[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
warn_unused_ignores = true
warn_redundant_casts = true
follow_imports = "silent"

[[tool.mypy.overrides]]
module = ["google.*", "googleapiclient.*", "pinecone", "anthropic.*"]
ignore_errors = true
```

Adding this is **out of scope** for the audit. Future cleanup work
should add it as part of a dedicated typing PR so the baseline
becomes enforceable in CI.
