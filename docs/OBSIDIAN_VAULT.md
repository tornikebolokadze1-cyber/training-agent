# Obsidian Knowledge Vault

## Purpose

The Obsidian vault (`obsidian-vault/`) is an automatically generated knowledge graph for the AI training course. After each lecture is processed through the analysis pipeline, key concepts, tools, relationships, and practical examples are extracted and written as interconnected markdown notes. Opening the vault in [Obsidian](https://obsidian.md/) provides a visual graph of how topics relate across all lectures and both training groups.

The vault serves two goals:

1. **Cross-lecture knowledge map** -- see which concepts appear in multiple lectures and how they connect.
2. **Searchable reference** -- every concept has a dedicated note with descriptions, lecture appearances, related topics, and practical examples.

---

## Directory Structure

```
obsidian-vault/
├── MOC - ძირითადი თემები.md          # Map of Content -- main index
├── კონცეფციები/                       # Concept notes (AI Agents, MCP, Vibe Coding, etc.)
├── ინსტრუმენტები/                     # Tool/platform notes (Claude, Gemini, n8n, etc.)
├── ლექციები/                          # Lecture notes with summaries and transcriptions
│   ├── ჯგუფი 1/                       #   Group 1 lectures (Tue/Fri)
│   └── ჯგუფი 2/                       #   Group 2 lectures (Mon/Thu)
├── ანალიზი/                           # Gap analysis + deep analysis per lecture
│   ├── ჯგუფი 1/
│   └── ჯგუფი 2/
├── WhatsApp დისკუსიები/               # WhatsApp group chat history (synced separately)
├── პრაქტიკული მაგალითები/             # Practical examples index (tool + use case pairs)
└── .obsidian/                          # Obsidian app config (graph colors, plugins)
```

### Note types

| Folder | Content | Tags |
|--------|---------|------|
| `კონცეფციები/` | One note per concept (category: concept, technique, methodology). Includes description, lecture appearances, related concepts, practical uses. | `კონცეფცია` |
| `ინსტრუმენტები/` | One note per tool or platform. Same structure as concepts. | `ინსტრუმენტი` |
| `ლექციები/ჯგუფი N/` | Full lecture note: summary, key points, concepts grouped by category, practical examples, relationships, collapsible transcript. | `ლექცია, ჯგუფი-N` |
| `ანალიზი/ჯგუფი N/` | Gap analysis and deep analysis reports for each lecture. | `ანალიზი, ჯგუფი-N` |
| `WhatsApp დისკუსიები/` | Chat history per group, synced from Green API. | -- |
| `პრაქტიკული მაგალითები/` | Single index note listing all practical examples by lecture. | `ინდექსი, პრაქტიკა` |

---

## Sync Mechanism

The sync is handled by `tools/integrations/obsidian_sync.py`. It supports three modes:

```bash
# Sync a single lecture (called automatically after pipeline)
python -m tools.integrations.obsidian_sync --group 1 --lecture 3

# Full rebuild from all Pinecone data
python -m tools.integrations.obsidian_sync --full

# Sync WhatsApp chat history
python -m tools.integrations.obsidian_sync --whatsapp
```

### Pipeline integration

The sync runs as **Step 7** of `transcribe_lecture.py`. It is wrapped in a try/except and marked **non-fatal** -- if entity extraction or file generation fails, the rest of the pipeline (notifications, delivery) is not affected.

```
Step 1: Download recording
Step 2: Gemini video transcription
Step 3: Claude reasoning analysis
Step 4: Gemini Georgian report writing
Step 5: Google Drive upload
Step 6: Pinecone knowledge indexing
Step 7: Obsidian vault sync  <-- non-fatal
Step 8: Notifications
```

### Single-lecture sync flow (`sync_lecture`)

1. **Extract from Pinecone** -- fetch all content types (transcript, summary, gap_analysis, deep_analysis) for the lecture. Also checks `.tmp/` for locally cached versions and uses whichever is larger.
2. **Entity extraction via Gemini** -- sends combined summary + deep_analysis text (up to 80K chars) to `gemini-2.5-flash` with a structured prompt. Returns JSON with concepts, relationships, key points, and practical examples.
3. **Validation** -- filters low-quality extractions: names must be 3-100 chars, descriptions 10+ chars, categories must be valid. Deduplicates within the same lecture using normalized names.
4. **Build cross-lecture concept index** -- loads all previously saved entity JSON files from `.tmp/entities/` and merges them. Normalizes names, picks the best display name, and aggregates descriptions, lecture appearances, and relationships across all lectures.
5. **Filter low-importance concepts** -- removes concepts that appear in only one lecture-group combo and have no relationships. This keeps the vault focused (typically 100-120 notes rather than 700+).
6. **Generate vault files** -- writes lecture notes, analysis notes, concept/tool notes, practical examples index, and the MOC.
7. **Clean up stale files** -- removes notes for concepts that are no longer in the filtered index.

### Full rebuild (`sync_full`)

Iterates over all groups and lectures, extracts from Pinecone, runs entity extraction for each, then performs a single merged vault generation. Used when the extraction prompt or canonical names change.

---

## Wikilinks and Graph Visualization

Every note uses Obsidian `[[wikilinks]]` to reference other notes. This creates the edges in Obsidian's graph view.

- Lecture notes link to their concepts: `[[AI Agents]]`, `[[Claude Code]]`
- Concept notes link back to lectures: `[[ლექცია 2]] (ჯგუფი 1)`
- Concept notes link to related concepts in a "დაკავშირებული" section
- Analysis notes link to their lecture: `[[ლექცია 3]]`
- The MOC links to everything

When opened in Obsidian, the graph view shows clusters around frequently discussed topics and reveals connections between lectures.

---

## MOC (Map of Content)

`MOC - ძირითადი თემები.md` is the main index file, auto-generated on every sync. It contains:

- **Lecture tables** -- one per group, with wikilinks to each lecture note and a topic summary.
- **AI Models section** -- links to all tool notes categorized as AI models.
- **Tools & Platforms section** -- links to tool/platform notes.
- **Core Concepts section** -- concepts that appear in 2+ lectures, with lecture reference codes (e.g., G1L2, G2L5).
- **Analysis links** -- per-group links to analysis notes.
- **Progress tracker** -- lectures completed vs total per group.

The MOC is regenerated fully on each sync to reflect any new lectures or concept changes.

---

## Canonical Naming System

Gemini's entity extraction produces inconsistent names across lectures (e.g., "Claude AI", "Claude (AI Model)", "Anthropic Claude" for the same tool). The sync module solves this with a canonical name mapping defined in `CANONICAL_NAMES` at the top of `obsidian_sync.py`.

### How it works

1. **Mapping dictionary** -- 100+ entries mapping variant names to a single canonical form:
   ```
   "claude (ai model)" -> "Claude"
   "claude ai" -> "Claude"
   "model context protocol (mcp)" -> "MCP (Model Context Protocol)"
   "google project idx / antigravity" -> "Google Antigravity / Project IDX"
   ```

2. **Normalization function** (`_normalize_concept_name`) -- lowercases the name, checks the canonical map, handles basic English plurals (e.g., "Agents" -> "Agent"), and returns a stable key for deduplication.

3. **Display name selection** (`_get_display_name`) -- returns the canonical display form. If no mapping exists, uses the most frequent variant seen across lectures, with ties broken by longest name.

4. **Aliases** -- non-canonical variants are stored in the note's YAML frontmatter as aliases, so Obsidian search can still find them.

### Adding new mappings

When Gemini produces a new inconsistent name variant, add it to `CANONICAL_NAMES` in `obsidian_sync.py` and run `--full` to rebuild:

```python
CANONICAL_NAMES: dict[str, str] = {
    # ... existing mappings ...
    "new variant name": "Canonical Name",
}
```

### Quality filters

Beyond canonical naming, these filters keep the vault focused:

- **Name length**: 3-100 characters
- **Description length**: minimum 10 characters
- **Valid categories only**: concept, tool, technique, platform, methodology
- **Multi-lecture requirement**: concepts appearing in only one lecture with no relationships are excluded
- **Category remapping**: common Gemini mistakes (e.g., "framework" -> "tool", "model" -> "tool") are auto-corrected

---

## Note Format

### Concept/Tool note structure

```markdown
---
tags: [კონცეფცია, AI]
aliases: [AI Agent, AI აგენტები]
category: concept
---

# AI Agents

> ქართულად: **AI აგენტი**

## აღწერა
[Merged descriptions from all lectures]

## ლექციებში
- [[ლექცია 2]] (ჯგუფი 1)
- [[ლექცია 3]] (ჯგუფი 1)
...

## დაკავშირებული
- [[Claude Code]]
- [[MCP (Model Context Protocol)]]
...

## პრაქტიკული გამოყენება
- [Tool] -- [Use case description] (ჯგუფი N, ლექცია M)
```

### Lecture note structure

```markdown
---
tags: [ლექცია, ჯგუფი-1]
date: 2026-03-20
group: 1
lecture: 3
---

# [Lecture Title]

## შეჯამება
[Summary text, up to 8000 chars]

## ძირითადი თემები
[Key points as bullet list]

### ინსტრუმენტები / კონცეფციები / ტექნიკები
[Wikilinked concepts grouped by category]

## პრაქტიკული მაგალითები
[Tool -- use case pairs]

## კავშირები
- წინა: [[ლექცია 2]]
- შემდეგი: [[ლექცია 4]]

## სრული ტრანსკრიფცია
> [Collapsible transcript block]
```

---

## Dependencies

- **Pinecone** -- source of lecture content (transcript, summary, analysis chunks).
- **Gemini 2.5 Flash** -- entity extraction model (fast, cost-effective for structured JSON extraction).
- **Green API** -- WhatsApp chat history for the `--whatsapp` sync mode.
- **Local `.tmp/` files** -- fallback/supplement for Pinecone data; also where entity JSON cache lives (`entities/` and `merged_data/` subdirectories).
