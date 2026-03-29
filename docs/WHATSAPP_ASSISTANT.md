# WhatsApp Assistant ("მრჩეველი")

The WhatsApp AI Assistant is an interactive bot embedded in the training group chats. It answers questions about AI, technology, and the course using a dual-model architecture that separates reasoning from Georgian-language writing.

---

## Architecture: Dual-Model Pipeline

The assistant uses two AI models in sequence, each playing a distinct role:

1. **Claude Opus 4.6** (decision + reasoning engine)
   - Decides whether the assistant should respond to a given message.
   - Produces a structured English bullet-list plan (3-5 key points) describing what the response should cover.
   - Does NOT write the final user-facing text.

2. **Gemini 3.1 Pro** (Georgian writer)
   - Takes Claude's reasoning plan and writes the actual Georgian-language reply.
   - Produces natural, conversational Georgian suitable for WhatsApp.
   - Falls back to a polite error message if the API call fails.

This separation exists because Claude excels at reasoning and decision-making, while Gemini produces higher-quality Georgian text.

---

## Message Flow

```
Incoming WhatsApp message
    |
    v
[1] Filter: skip own messages, skip empty/media-only
    |
    v
[2] Record message in chat history buffer
    |
    v
[3] Detect trigger type (direct or passive)
    |
    v
[4] Cooldown check (passive only) — skip if on cooldown
    |
    v
[5] Resolve training group number (1 or 2) from chat ID
    |
    v
[6] Retrieve course context from Pinecone RAG (top_k=4)
    |
    v
[6.5] Recall personal memories from Mem0 (limit=3)
    |
    v
[7] Claude Opus 4.6: decide + reason
    |   - Returns key-points plan, or None (stay silent)
    |
    v
[7.5] Web search via Gemini Google Search grounding
    |   - Enriches response with real-time info
    |
    v
[8] Gemini 3.1 Pro: write Georgian response
    |
    v
[9] Format: prepend footer signature
    |
    v
[10] Send via Green API (WhatsApp)
    |
    v
[11] Update cooldown timer (passive responses only)
    |
    v
[12] Save interaction to Mem0 memory
```

---

## Trigger System

### Direct Trigger

The assistant always responds when directly addressed. Detection is case-insensitive and matches:

- `მრჩეველო` (Georgian vocative — primary trigger word)
- `მრჩეველი` (Georgian nominative)
- `mrchevelo` (Latin transliteration)
- `mrcheveli` (Latin transliteration)

When a direct trigger is detected, Claude is instructed to **always** produce a response plan (never return SILENT).

On Claude API errors during direct mentions, `alert_operator()` sends a WhatsApp notification to the operator as a last-resort escalation.

### Passive Trigger

When no direct trigger word is found, Claude evaluates the message in context and decides whether to respond. Claude **responds** when:

- Someone asks a question about AI, technology, tools, or the course
- Someone shares a problem or confusion about tech topics
- An AI/tech discussion is happening and the assistant can add value
- Someone asks for help, advice, or recommendations about tools

Claude stays **silent** for:

- Pure greetings with no question ("გამარჯობა", "სალამი")
- Simple reactions ("კარგი", "მადლობა", emoji-only messages)
- Personal or off-topic conversations clearly between humans
- Questions that have already been fully answered by someone else

The default bias is to respond: "When in doubt, RESPOND -- it's better to help than to stay silent."

---

## Cooldown System

Prevents the assistant from flooding group chats with unsolicited (passive) responses.

| Parameter | Value |
|-----------|-------|
| Scope | Per-chat (each group chat has its own cooldown) |
| Duration | `ASSISTANT_COOLDOWN_SECONDS` = 300 (5 minutes) |
| Applies to | Passive responses only (direct mentions bypass cooldown) |

After sending a passive response, the assistant records the current timestamp for that chat. Subsequent passive triggers within the cooldown window are silently skipped.

---

## Chat History

The assistant maintains a rolling buffer of recent messages per chat to give Claude conversational context.

| Parameter | Value |
|-----------|-------|
| Messages per chat | 15 (most recent kept) |
| Max tracked chats | 50 (LRU eviction beyond this) |
| Context shown to Claude | Last 12 messages (excluding current) |
| Message fields stored | sender name, text (truncated to 500 chars), timestamp |

### LRU Eviction

When the number of tracked chats exceeds 50, the oldest half (sorted by most recent message timestamp) is evicted. This clears both chat history and cooldown timers for those chats.

---

## Pinecone RAG (Course Knowledge)

Before Claude makes its decision, the assistant queries the Pinecone knowledge base for relevant course content.

| Parameter | Value |
|-----------|-------|
| Query | The message text |
| top_k | 4 results |
| Group filter | Filters by training group number (1 or 2) when known |
| Result format | Lecture number, content type, relevance score, text chunk (up to 600 chars each) |

The retrieved context is injected into both Claude's decision prompt and Gemini's writing prompt, allowing responses to reference specific lecture material.

---

## Mem0 Personal Memory System

The assistant learns from interactions using Mem0, a personal memory layer that remembers user preferences, past questions, and conversation patterns.

### Deployment Modes

Mem0 supports four deployment configurations, selected automatically based on available environment variables:

| Mode | Vector Store | Graph Store | When Selected |
|------|-------------|-------------|---------------|
| `cloud-full` | Qdrant Cloud | Neo4j AuraDB | Both `QDRANT_URL`+`QDRANT_API_KEY` and `NEO4J_URL`+`NEO4J_USERNAME`+`NEO4J_PASSWORD` are set |
| `cloud-qdrant` | Qdrant Cloud | None | Only Qdrant credentials are set |
| `cloud-neo4j` | Local Qdrant | Neo4j AuraDB | Only Neo4j credentials are set |
| `local` | Local Qdrant (in-memory) | None | No cloud credentials set (fallback) |

### Embeddings

Mem0 uses **Gemini embeddings** (`gemini-embedding-001`) with **768 dimensions**. This is different from OpenAI's default 1536-dimensional embeddings. The embedding dimensions must be set explicitly in the Qdrant vector store config to avoid dimension mismatch errors.

### LLM Provider

Mem0 uses **Gemini 2.5 Flash** as its LLM (for memory extraction and management), reusing the same Gemini API key configured for the project. No separate OpenAI key is required.

### Memory Operations

- **Recall**: Before Claude's decision step, the assistant searches Mem0 for up to 3 relevant memories about the current user/topic. These are injected into the context as `MEMORY (previous interactions with this user)`.
- **Save**: After sending a response, the full conversation turn (user message + assistant response) is saved to Mem0 under the user's ID (sender name or truncated sender ID).
- **Failure handling**: Memory operations are non-critical. Failures are logged at debug level and do not interrupt the message pipeline.

---

## Web Search

The assistant enriches responses with real-time information using Gemini with Google Search grounding.

| Parameter | Value |
|-----------|-------|
| Model | Gemini 2.5 Flash |
| Tool | `GoogleSearch` grounding |
| Temperature | 0.1 (factual) |
| Result cap | 2000 characters |

### When Web Search Runs

Web search runs for all substantive messages by default. It is skipped only for very short messages (under 30 characters) that match simple patterns like greetings or acknowledgments ("გამარჯობა", "მადლობა", "კარგი", "ok").

---

## Input Sanitization

All user input is sanitized before being passed to any LLM:

- **Control characters removed**: Null bytes and other control chars (`\x00-\x08`, `\x0b`, `\x0c`, `\x0e-\x1f`, `\x7f`) are stripped. Newlines and tabs are preserved.
- **Length truncation**: Messages are truncated to **4000 characters** maximum, with a `... [truncated]` suffix appended.
- **Prompt injection defense**: Claude's system prompt explicitly marks user messages as "untrusted data" and instructs it not to follow instructions embedded within messages.

---

## Retry Logic

### Claude (Decision + Reasoning)

| Parameter | Value |
|-----------|-------|
| Max retries | 3 |
| Wait times | 30 seconds, 60 seconds (between attempts) |
| Retry trigger | `RateLimitError` only |
| Non-retryable | All other `APIError` subtypes (fail immediately) |

### Gemini (Georgian Writing)

| Parameter | Value |
|-----------|-------|
| Max retries | 3 |
| Wait times | 15 seconds, 30 seconds (between attempts) |
| Retry trigger | Rate limit / quota errors (detected by keyword: "rate", "limit", "429", "quota", "resource_exhausted") |
| Fallback on failure | Returns a polite Georgian error message: "ბოდიში, ამჯერად ვერ მოვახერხე პასუხის გენერირება. სცადეთ მოგვიანებით." |

A 1-second pause is inserted between the Claude and Gemini calls to reduce burst pressure on shared API quotas.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Claude API error on **direct mention** | `alert_operator()` sends WhatsApp notification to the operator |
| Claude API error on **passive trigger** | Logged as error, assistant stays silent |
| Gemini API error | Returns fallback Georgian error message |
| Pinecone retrieval failure | Logged as warning, continues without course context |
| Mem0 recall/save failure | Logged at debug level, non-blocking |
| Web search failure | Logged as warning, continues without web context |
| Message send failure | Logged as error, returns None |

---

## Response Format

Every response follows this structure:

```
🤖 AI ასისტენტი - მრჩეველი
---
[Georgian response text]
```

The footer signature is prepended (not appended) to the response body.

### Writing Rules (Gemini Prompt)

- Natural, casual Georgian -- like a smart friend chatting, not a textbook
- Short: 2-3 sentences maximum (WhatsApp-appropriate length)
- No emojis in the response body
- Formal "თქვენ" (you-plural/formal), never "შენ" (informal)
- No self-introduction, no "გამარჯობა"
- No disclaimers about being an AI
- Goes straight to the point with a clear opinion or angle

---

## Quoted Message Support

When a user replies to a previous message, the quoted text is included in Claude's context with the prefix: `[This message is a REPLY to a previous message. The quoted message was: "..."]`. This allows Claude to understand conversational threads and respond appropriately to reply chains.

---

## CLI Testing

The module includes a CLI entrypoint for smoke testing:

```bash
# Dry run (shows config, no API calls)
python -m tools.services.whatsapp_assistant

# Live test (actually calls APIs and sends a message)
python -m tools.services.whatsapp_assistant --live '<chat_id>' '<sender_id>' '<sender_name>' '<message_text>'
```

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | Yes | Claude Opus 4.6 API access |
| `GEMINI_API_KEY` | Yes | Gemini API access (free tier) |
| `GEMINI_API_KEY_PAID` | No | Paid Gemini key for 3.1 Pro (falls back to `GEMINI_API_KEY`) |
| `QDRANT_URL` | No | Qdrant Cloud endpoint for Mem0 |
| `QDRANT_API_KEY` | No | Qdrant Cloud API key for Mem0 |
| `NEO4J_URL` | No | Neo4j AuraDB URL for Mem0 graph store |
| `NEO4J_USERNAME` | No | Neo4j username |
| `NEO4J_PASSWORD` | No | Neo4j password |
| `WHATSAPP_GROUP1_ID` | No | Group 1 WhatsApp chat ID (for group detection) |
| `WHATSAPP_GROUP2_ID` | No | Group 2 WhatsApp chat ID (for group detection) |

Configuration constants are centralized in `tools/core/config.py`:
- `ASSISTANT_CLAUDE_MODEL` -- Claude model ID
- `ASSISTANT_COOLDOWN_SECONDS` -- passive response cooldown (default: 300)
- `ASSISTANT_SIGNATURE` -- footer text
- `ASSISTANT_TRIGGER_WORD` -- primary Georgian trigger word
- `GEMINI_MODEL_ANALYSIS` -- Gemini model ID for writing
