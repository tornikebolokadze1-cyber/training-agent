# 007: Claude Opus for Analysis Reasoning

## Date
2026-03-18

## Status
superseded by inline change in `tools/core/config.py:558` (2026-03 cost-saving switch to Sonnet 4.6). See **Update (2026-05-13)** below.

## Context
The lecture analysis pipeline needs to produce deep, nuanced analysis of teaching quality — identifying knowledge gaps, comparing lecture content to course objectives, and generating actionable improvement suggestions. This requires sophisticated reasoning beyond what Gemini alone provides.

## Decision
Use **Claude Opus 4.6 with extended thinking** as the reasoning engine in the analysis pipeline.

Configuration:
- Model: `claude-opus-4-6` (configured as `ASSISTANT_CLAUDE_MODEL` in config.py)
- API key: `ANTHROPIC_API_KEY` env var
- Used in: analysis stage of `transcribe_lecture.py` pipeline and WhatsApp assistant reasoning

## Reasoning
1. **Claude for reasoning**: Claude excels at nuanced analysis, identifying patterns across content, and structured analytical thinking. Extended thinking mode allows deeper reasoning chains.
2. **Not Claude for writing**: Claude's Georgian language output is adequate but not native-quality. Gemini 3.1 Pro Preview produces significantly better Georgian text.
3. **Pipeline separation**: By separating reasoning (Claude) from writing (Gemini), each model does what it does best. Claude's English-language analysis output becomes input for Gemini's Georgian writing.
4. **WhatsApp assistant**: Same Claude model used for მრჩეველი reasoning — analyzes the question + retrieved RAG context, then hands off to Gemini for Georgian response.

## Consequences
- **Positive**: Highest-quality analysis reasoning. Extended thinking catches nuanced teaching issues. Consistent with assistant reasoning architecture.
- **Negative**: Additional API cost (Opus is expensive). Two-API-call overhead per analysis.
- **Trade-off**: Analysis quality is the #1 priority (per user's priorities: quality > stability > security). Cost is acceptable for 30 total lectures.

---

## Update (2026-05-13) — Switched to Sonnet 4.6

`ASSISTANT_CLAUDE_MODEL` in `tools/core/config.py:558` ships `claude-sonnet-4-6`, not Opus. The switch was made earlier (~2 months before this audit) for ~$150/course savings with minimal quality loss in observed Georgian analysis output. Both the analysis pipeline (`_claude_reason` in `tools/integrations/gemini_analyzer.py`) and the WhatsApp assistant (`tools/services/whatsapp_assistant.py`) use this same constant, so both run on Sonnet 4.6.

The original Opus decision and its reasoning above are preserved for historical context. The current production state is Sonnet 4.6; this ADR is marked **superseded** rather than rewritten so the rationale chain (Opus → Sonnet for cost) remains traceable.

No replacement ADR was written at the time of the switch — this update closes that gap.
