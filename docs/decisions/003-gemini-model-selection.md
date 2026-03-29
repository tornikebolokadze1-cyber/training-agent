# 003: Gemini Model Selection for Analysis Pipeline

## Date
2026-03-25

## Status
accepted (supersedes earlier decision to use Gemini 2.5 Pro for transcription)

## Context
The lecture analysis pipeline needs two distinct Gemini capabilities:
1. **Transcription**: Process 2-hour lecture videos multimodally (see slides, hear audio)
2. **Georgian text generation**: Write polished Georgian-language reports from Claude's analysis

Initially, Gemini 2.5 Pro was used for transcription. On 2026-03-25, this was changed to Gemini 2.5 Flash for cost optimization.

## Decision
- **Transcription**: `gemini-2.5-flash` — cheaper (8-17x vs Pro), sufficient for video understanding
- **Georgian text writing**: `gemini-3.1-pro-preview` — best available for Georgian language generation

Configuration in `tools/core/config.py`:
```python
GEMINI_MODEL_TRANSCRIPTION = "gemini-2.5-flash"
GEMINI_MODEL_ANALYSIS = "gemini-3.1-pro-preview"
```

## Reasoning
1. **Flash over Pro for transcription**: Transcription is a relatively straightforward task — extract what's said and shown. Flash handles this well and costs 8-17x less. With 15 lectures × 2 groups × 2+ hours each, the cost difference is significant.
2. **3.1 Pro Preview for Georgian**: Georgian is a low-resource language. The most capable model available produces significantly better Georgian text. Quality matters here because these reports go to course participants.
3. **Video chunking**: Both models have a 1M token limit. Videos are split into ~45-minute chunks via ffmpeg (no re-encoding), each chunk ≈783K tokens.

## Consequences
- **Cost savings**: ~85-95% reduction in transcription costs vs Pro
- **Quality maintained**: Flash transcription quality is sufficient; Georgian writing quality preserved with Pro
- **Risk**: If Flash quality drops on future updates, may need to revert to Pro for transcription
- **Monitoring**: Compare transcription quality periodically between Flash and Pro
