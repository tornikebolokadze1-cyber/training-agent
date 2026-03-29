# 008: მრჩეველი Memory System Overhaul

## Date
2026-03-29

## Status
accepted — Phase 1-2 implemented (2026-03-29), Phase 3 partially implemented (chat_store.py and Georgian chunking pending)

## Context
Deep audit by 6 specialized agents found 17 issues (2 CRITICAL, 7 HIGH, 8 MEDIUM) across the მრჩეველი assistant's 3 memory systems: Mem0, Pinecone RAG, and chat history. The issues fall into 3 categories: persistence failures, context pipeline architecture, and prompt quality.

## Decision
Implement a comprehensive overhaul in 3 phases:

### Phase 1: Critical Fixes (< 30 min, no architecture change)
1. **Fix user_id** — use `sender_id` instead of `sender_name` (1 new method)
2. **Fix thread-safety** in `get_pinecone_index()` — move init inside lock
3. **Remove 600-char truncation** in `_retrieve_context` — pass full chunks
4. **Raise MIN_RELEVANCE_SCORE** from 0.45 to 0.55
5. **Sanitize memory content** — apply `_sanitize_input` to Mem0 recalls
6. **Exclude deep_analysis** from student-facing RAG queries

### Phase 2: Context Pipeline Restructuring (1-2 hours)
7. **Split context types** — separate Pinecone, Mem0, and web search into distinct variables
8. **Update Claude system prompt** — add COURSE KNOWLEDGE and USER HISTORY sections
9. **Update Gemini writing prompt** — add structured CONTEXT USAGE instructions
10. **Add personalization hints** — Claude reasoning includes "Personalization:" line
11. **Record assistant responses** in chat history
12. **Increase text truncation** from 200 to 500 chars in history
13. **Adaptive response length** — 1-2 sentences for simple, 5-6 for complex
14. **Protect group chats** from LRU eviction
15. **Differentiate buffer sizes** — 40 messages for groups, 15 for private

### Phase 3: Persistence & Learning (2-3 hours)
16. **Chat history persistence** — SQLite module mirroring analytics.py pattern
17. **Memory save quality filter** — skip trivial interactions (< 20 chars, greetings)
18. **Mem0 metadata enrichment** — add group_number, topic, interaction_count
19. **Railway enforcement** — require cloud Qdrant vars in production, warn if local
20. **Georgian-aware chunking** — split on paragraph/sentence boundaries, CHARS_PER_TOKEN=6

## Reasoning
- Phase 1 fixes are code-level changes with zero architecture risk
- Phase 2 is the highest-impact change — makes memory actually USABLE by the AI
- Phase 3 adds durability and learning quality improvements

## Consequences
- Responses will be more personalized (memory-informed)
- Context quality improves ~3x (full chunks, structured sections)
- No more data loss on restart (chat history persisted)
- No more user_id collisions (stable sender_id)
- Minor API cost increase from larger context windows (negligible vs. Opus costs)

## Files to Modify
- `tools/services/whatsapp_assistant.py` — main changes (phases 1-2)
- `tools/integrations/knowledge_indexer.py` — thread-safety, chunking, relevance
- `tools/services/chat_store.py` — NEW: SQLite chat persistence
- `tools/app/server.py` — handle_message context restructuring
- `tools/core/config.py` — add CHARS_PER_TOKEN_KA constant
