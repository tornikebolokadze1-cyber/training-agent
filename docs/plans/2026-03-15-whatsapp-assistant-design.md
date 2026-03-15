# WhatsApp AI Assistant ("მრჩეველი") — Design Document

## Overview
Interactive AI assistant that lives in the training group WhatsApp chats. Monitors conversations, responds to direct mentions ("მრჩეველო"), and selectively joins AI-related discussions when it can add value.

## Architecture: Dual-Model Pipeline

```
Group message → Green API webhook → POST /whatsapp-incoming
    → Filter (ignore own messages, media-only, spam)
    → Decision + Reasoning (Claude Opus 4.6):
        - Query Pinecone for relevant course context
        - Decide: respond or stay silent?
        - If respond: produce key points + reasoning
    → Response Writing (Gemini 3.1 Pro):
        - Takes Claude's reasoning + Pinecone context
        - Writes natural Georgian response
    → Append footer: "---\nAI ასისტენტი - მრჩეველი"
    → Send via Green API → group chat
```

## Response Logic

### Always respond:
- Message contains "მრჩეველო" (direct address)

### Consider responding (confidence threshold):
- AI/ML/automation questions in the group
- Misconceptions about AI topics
- Discussion about lecture material
- Someone confused about course content

### Never respond:
- Casual greetings, jokes, off-topic
- Media-only messages without AI context
- When another member already answered correctly
- Cooldown: max 1 passive response per 5 minutes

## Knowledge Base (Pinecone)

- **Index**: `training-course`
- **Embedding model**: Gemini `text-embedding-004`
- **Content indexed**: lecture transcripts, summaries, gap analyses, course metadata
- **Metadata**: `group_number`, `lecture_number`, `content_type`, `date`
- **Chunk size**: ~500 tokens with overlap
- **Indexing trigger**: automatic after each lecture processing

## Message Format

```
(response body — no emojis)
---
AI ასისტენტი - მრჩეველი
```

## Models Used
- **Claude Opus 4.6** (`claude-opus-4-6`): reasoning, decision-making, response planning
- **Gemini 3.1 Pro** (`gemini-3.1-pro-preview`): Georgian language response writing
- **Gemini text-embedding-004**: vector embeddings for Pinecone

## Files
- `tools/whatsapp_assistant.py` — main assistant logic (dual-model pipeline)
- `tools/knowledge_indexer.py` — Pinecone indexing pipeline
- `tools/server.py` — new `/whatsapp-incoming` endpoint
- `tools/config.py` — new config: ANTHROPIC_API_KEY, PINECONE_API_KEY

## Safety
- Infinite loop prevention (ignore own messages by sender ID)
- Rate limiting (cooldown between passive responses)
- Message length cap (WhatsApp-appropriate, concise)
- No emojis in responses
- Georgian language only for responses
