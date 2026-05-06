"""Obsidian vault synchronization for the Training Agent.

Extracts lecture data from Pinecone, uses Gemini to extract entities and
relationships, and generates/updates Obsidian markdown files with wikilinks
for graph visualization.

Usage:
    # Sync a specific lecture after pipeline completes
    python -m tools.integrations.obsidian_sync --group 1 --lecture 3

    # Rebuild entire vault from all Pinecone data
    python -m tools.integrations.obsidian_sync --full

    # Sync WhatsApp chat history (requires active Green API session)
    python -m tools.integrations.obsidian_sync --whatsapp
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter, defaultdict
from typing import Any

from tools.core.config import (
    ANTHROPIC_API_KEY,
    GREEN_API_INSTANCE_ID,
    GREEN_API_TOKEN,
    GROUPS,
    PROJECT_ROOT,
    TMP_DIR,
    WHATSAPP_GROUP1_ID,
    WHATSAPP_GROUP2_ID,
)
from tools.core.retry import retry_with_backoff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAULT_ROOT = PROJECT_ROOT / "obsidian-vault"
ENTITIES_DIR = TMP_DIR / "entities"
MERGED_DIR = TMP_DIR / "merged_data"

# Approximate lecture dates by group (computed from start_date + meeting_days)
# These are recalculated dynamically in _compute_lecture_date()

CONTENT_TYPES = ("transcript", "summary", "gap_analysis", "deep_analysis")

# Categories that map to the "ინსტრუმენტები" folder
TOOL_CATEGORIES = frozenset({"tool", "platform"})

# Gemini model for entity extraction (fast + cheap)
ENTITY_EXTRACTION_MODEL = "gemini-2.5-flash"

VALID_CATEGORIES = frozenset({"concept", "tool", "technique", "platform", "methodology"})

# ---------------------------------------------------------------------------
# Canonical name mapping — resolves Gemini's inconsistent naming across lectures
# Keys are normalized (lowercase, stripped), values are the canonical display name.
# ---------------------------------------------------------------------------
CANONICAL_NAMES: dict[str, str] = {
    # --- Tools / Platforms: Claude variants ---
    "claude (ai model)": "Claude",
    "claude ai": "Claude",
    # Claude Desktop variants
    "claude desktop app": "Claude Desktop",
    "claude desktop application": "Claude Desktop",
    # Claude Code variants
    "claude code (development framework)": "Claude Code",
    # Claude Skills variants
    "ai skills (claude skills)": "Claude Skills",
    "skills (ai agent skills)": "Claude Skills",
    "skills (ai)": "Claude Skills",
    "skill": "Claude Skills",
    # --- Tools: Kimi ---
    "kimi (model)": "Kimi",
    # --- Tools: VS Code ---
    "vs code (visual studio code)": "VS Code",
    # --- Tools: Google IDX / Antigravity ---
    "google project idx / antigravity": "Google Antigravity / Project IDX",
    "google antigravity ide": "Google Antigravity / Project IDX",
    # --- Tools: Google Gemini variants ---
    "google gemini": "Gemini",
    "google gemini gems": "Gemini Gems",
    # --- Garbled / malformed names ---
    "airmpi.com/skills": "airmpi.com/skills",  # will be filtered by length/quality checks
    # --- Concepts: MCP variants ---
    "model context protocol (mcp)": "MCP (Model Context Protocol)",
    "model context protocol (mcp) servers": "MCP (Model Context Protocol)",
    # --- Concepts: Computer Use ---
    "computer use (ai)": "Computer Use",
    # --- Concepts: Deep Research ---
    "deep research (ai)": "Deep Research",
    # --- Concepts: Custom Instructions ---
    "custom instructions (ai)": "Custom Instructions",
    # --- Concepts: API variants ---
    "api access": "API (Access & Keys)",
    "api connection": "API (Access & Keys)",
    "api key": "API (Access & Keys)",
    "api key generation": "API (Access & Keys)",
    "api keys": "API (Access & Keys)",
    "direct api integration": "API (Access & Keys)",
    # --- Concepts: CLAUDE.md (config concept) ---
    "claude.md": "CLAUDE.md",
    # --- Concepts: Memory Management variants ---
    "memory and context management": "Memory Management (AI)",
    "memory management": "Memory Management (AI)",
    "ai memory": "Memory Management (AI)",
    # --- Concepts: Data Privacy variants ---
    "data control / privacy": "Data Privacy & Control",
    "ai data control": "Data Privacy & Control",
    "data control  privacy": "Data Privacy & Control",
    # --- Concepts: System Prompt / Developer Prompt ---
    "developer/system prompt": "System Prompt",
    # --- Concepts: Open Source variants ---
    "open source models": "Open Source",
    # --- Concepts: Agent variants ---
    "ai agent": "AI Agents",
    "agent vs. tool distinction (ai)": "AI Agents",
    # --- Concepts: LLMs ---
    "large language models (llms)": "Large Language Models (LLMs)",
    # --- Concepts: PRD (appears in both concept and methodology) ---
    "product requirements document (prd)": "Product Requirements Document (PRD)",
    # --- Techniques: Session Management ---
    "session management (ai)": "Session Management",
    # --- Techniques: Claude Desktop Dispatch ---
    "claude desktop dispatch": "Claude Desktop",
    # --- Tools: Perplexity variants ---
    "perplexity comet": "Perplexity",
    "perplexity computer": "Perplexity",
    # --- Platforms: Claude Web ---
    "claude web interface": "Claude",
    "claude website": "Claude",
    # --- Platforms: ChatGPT (appears in both tool & platform) ---
    # Keep as-is, no mapping needed — just ensure consistency
    # --- Techniques: Git variants ---
    "commit (git commit)": "Git",
    "push (git push)": "Git",
    # --- Concepts: Version Control ---
    "version control system (vcs)": "Version Control",
    # --- Concepts: IDE ---
    "ide (integrated development environment)": "IDE",
    # --- Techniques: VS Code Security ---
    "vs code security settings": "VS Code",
    "visual studio code (vs code)": "VS Code",
    # --- Claude Chat/Cowork/Code Desktop modes (keep Claude Desktop) ---
    "claude chat": "Claude Desktop",
    "claude cowork": "Claude Desktop",
    "claude code (desktop interface)": "Claude Code",
    "claude extension": "Claude",
    "claude-code-setup repository": "Claude Code",
    # --- Skills consolidation ---
    "ai skills": "Claude Skills",
    "agent skills": "Claude Skills",
    "skills (ai models)": "Claude Skills",
    # --- MCP Servers variant ---
    "mcp (model context protocol) servers": "MCP (Model Context Protocol)",
    # --- Google variants ---
    "google project idx/antigravity": "Google Antigravity / Project IDX",
    "google project idxantigravity": "Google Antigravity / Project IDX",
    "google labs": "Google AI Studio",
    "stitch": "Google AI Studio",
    "gemini gems": "Gemini",
    "gemini canvas": "Gemini",
    # --- Perplexity ---
    "supergrok": "Grok",
    # --- Make/Zapier → n8n category ---
    "make.com": "Make",
    # --- CLI variants ---
    "anything cli": "CLI-Anything",
    "playwright cli": "Playwright",
    # --- Concepts: duplicates ---
    "agentic ai workflows": "AI Agents",
    "agent orchestration": "AI Agents",
    "facebook developer graph api": "WhatsApp Integration",
    "green api": "WhatsApp Integration",
    "manychat": "WhatsApp Integration",
    # --- Round 3: new Gemini variants from re-sync ---
    "notebooklm (google)": "NotebookLM",
    "vs code claude extension": "Claude",
    "vs code codex extension": "VS Code",
    "visual studio code": "VS Code",
    "mcp servers (model context protocol)": "MCP (Model Context Protocol)",
    "mcp": "MCP (Model Context Protocol)",
    "ai skill": "Claude Skills",
    "claude code skills and tools": "Claude Skills",
    "skills (for claude code)": "Claude Skills",
    "google project idx": "Google Antigravity / Project IDX",
    "google cloud": "Google AI Studio",
    "aws (amazon web services)": "AWS",
    "microsoft azure": "Azure",
    "microsoft excel": "Excel",
    "deeplearning.ai": "DeepLearning.AI",
    "enagram ai": "Enagram AI",
    "genspark ai": "Genspark AI",
    "draw.io": "Draw.io",
    "speech-to-text system": "Speech-to-Text",
    "rube mcp server": "MCP (Model Context Protocol)",
    "third-party scraping services": "Web Scraping",
}

# Words that indicate basic English plurals (but NOT words ending in "sis", "is", etc.)
_NO_DEPLURAL = frozenset({
    "analysis", "basis", "thesis", "diagnosis", "synthesis", "hypothesis",
    "kubernetes", "atlas", "canvas", "bias", "alias", "status", "corpus",
    "apis", "series", "species",
})

ENTITY_EXTRACTION_PROMPT = """Analyze this AI course lecture content and extract the 15-30 MOST IMPORTANT concepts.
The content is in Georgian. Return ONLY valid JSON (no markdown code blocks) with this structure:
{
  "lecture_title": "lecture title in Georgian",
  "date": "estimated date if mentioned, or empty string",
  "concepts": [
    {"name": "concept name (English for tech terms)", "name_ka": "Georgian name if applicable", "description": "brief description in Georgian (min 2 sentences)", "category": "concept|tool|technique|platform|methodology"}
  ],
  "relationships": [
    {"from": "entity1", "to": "entity2", "type": "uses|explains|compares|requires|part_of|alternative_to|integrates_with"}
  ],
  "key_points": ["key point in Georgian"],
  "practical_examples": [
    {"tool": "tool name", "use_case": "use case description in Georgian"}
  ],
  "people_mentioned": [
    {"name": "person name", "role": "their role or affiliation"}
  ]
}

STRICT RULES:
1. Extract ONLY 15-30 concepts that were EXPLAINED or DEMONSTRATED for 2+ minutes. Not passing mentions.
2. Category MUST be exactly one of: concept, tool, technique, platform, methodology. No other values.
3. Use ONE canonical English name per concept (e.g., "Claude" not "Claude AI" or "Claude Opus" or "Anthropic Claude").
   Group product features under the parent tool (e.g., NotebookLM's audio overview → goes under "NotebookLM").
4. EXCLUDE:
   - Company names used only as examples (Booking.com, Binance, Netflix, etc.)
   - Course meta-topics (pedagogy, learning objectives, homework, grading)
   - Generic IT terms everyone knows (frontend, backend, localhost, server, browser, API call)
   - Individual product features as separate concepts — merge them into the parent tool
5. Keep descriptions in Georgian, minimum 2 sentences each.
6. Relationships: only between concepts you actually extracted. Both endpoints must be in your concepts list.

CONTENT:
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_filename(name: str) -> str:
    """Remove characters not allowed in filenames."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()[:100]


def _wikilink(name: str) -> str:
    """Return an Obsidian wikilink."""
    return f"[[{name}]]"


def _compute_lecture_date(group_number: int, lecture_number: int) -> str:
    """Compute the ISO date for a given group + lecture number."""
    from datetime import timedelta

    group = GROUPS.get(group_number)
    if not group:
        return ""

    start = group["start_date"]
    meeting_days = group["meeting_days"]

    count = 0
    current = start
    max_days = 365  # safety limit
    while count < lecture_number and max_days > 0:
        if current.weekday() in meeting_days:
            count += 1
            if count == lecture_number:
                return current.isoformat()
        current += timedelta(days=1)
        max_days -= 1
    return ""


def _parse_lecture_key(key: str) -> tuple[int, int]:
    """Parse 'g1_l2' into (1, 2)."""
    parts = key.split("_")
    return int(parts[0][1:]), int(parts[1][1:])


def _normalize_concept_name(name: str) -> str:
    """Normalize a concept name for deduplication.

    Returns a lowercase, stripped key. Checks the canonical name mapping
    first, then handles basic English plurals (e.g., "Agents" -> "agent")
    but avoids breaking words like "Analysis".
    """
    key = name.strip().lower()
    # Check canonical mapping before any other normalization
    if key in CANONICAL_NAMES:
        return CANONICAL_NAMES[key].strip().lower()
    # Handle basic English plurals: trailing 's' removal
    if (
        key.endswith("s")
        and not key.endswith("ss")
        and len(key) > 3
        and key not in _NO_DEPLURAL
    ):
        key = key[:-1]
    # Check canonical mapping again after depluralization
    if key in CANONICAL_NAMES:
        return CANONICAL_NAMES[key].strip().lower()
    return key


def _get_display_name(name: str) -> str:
    """Return the canonical display name for a concept.

    If the name has a canonical mapping, returns the proper display form.
    Otherwise returns the original name (stripped).
    """
    key = name.strip().lower()
    if key in CANONICAL_NAMES:
        return CANONICAL_NAMES[key]
    return name.strip()


def _validate_entities(data: dict[str, Any]) -> dict[str, Any]:
    """Filter low-quality extractions after Gemini returns.

    Enforces name length, description length, valid categories,
    and deduplicates concepts within the same lecture.
    """
    if not data or "concepts" not in data:
        return data

    raw_count = len(data.get("concepts", []))
    seen_keys: set[str] = set()
    valid_concepts: list[dict[str, Any]] = []

    for concept in data.get("concepts", []):
        name = concept.get("name", "").strip()
        # Name: 3-100 chars
        if not name or len(name) < 3 or len(name) > 100:
            continue
        # Description: minimum 10 chars
        desc = concept.get("description", "").strip()
        if len(desc) < 10:
            continue
        # Category: must be valid
        cat = concept.get("category", "").strip().lower()
        if cat not in VALID_CATEGORIES:
            # Try to remap common mistakes
            remap = {
                "framework": "tool", "library": "tool", "service": "platform",
                "model": "tool", "ai_model": "tool", "ai model": "tool",
                "principle": "concept", "theory": "concept", "approach": "methodology",
                "method": "technique", "strategy": "methodology", "pattern": "technique",
            }
            cat = remap.get(cat, "concept")
        concept["category"] = cat
        concept["name"] = name
        concept["description"] = desc

        # Case-insensitive dedup within same lecture
        norm_key = _normalize_concept_name(name)
        if norm_key in seen_keys:
            continue
        seen_keys.add(norm_key)
        valid_concepts.append(concept)

    data["concepts"] = valid_concepts

    # Validate relationships: both endpoints must exist
    valid_names = {_normalize_concept_name(c["name"]) for c in valid_concepts}
    valid_rels: list[dict[str, str]] = []
    for rel in data.get("relationships", []):
        from_n = _normalize_concept_name(rel.get("from", ""))
        to_n = _normalize_concept_name(rel.get("to", ""))
        if from_n in valid_names and to_n in valid_names:
            valid_rels.append(rel)
    data["relationships"] = valid_rels

    filtered = raw_count - len(valid_concepts)
    logger.info(
        "Validated %d/%d concepts (%d filtered)",
        len(valid_concepts), raw_count, filtered,
    )
    return data


# ---------------------------------------------------------------------------
# Step 1: Extract data from Pinecone
# ---------------------------------------------------------------------------


def extract_from_pinecone(
    group_number: int,
    lecture_number: int,
) -> dict[str, str]:
    """Extract all content types for a lecture from Pinecone.

    Returns:
        Dict mapping content_type -> full_text.
    """
    from tools.integrations.knowledge_indexer import get_pinecone_index

    idx = get_pinecone_index()
    results: dict[str, str] = {}

    for ctype in CONTENT_TYPES:
        prefix = f"g{group_number}_l{lecture_number}_{ctype}_"
        ids: list[str] = []

        for page in idx.list(prefix=prefix, limit=100):
            ids.extend(page)

        if not ids:
            logger.debug("No vectors for %s", prefix)
            continue

        # Fetch in batches of 100
        all_chunks: list[tuple[int, str]] = []
        for i in range(0, len(ids), 100):
            batch = ids[i : i + 100]
            fetched = idx.fetch(ids=batch)
            for v in fetched.vectors.values():
                chunk_idx = v.metadata.get("chunk_index", 0)
                text = v.metadata.get("text", "")
                all_chunks.append((chunk_idx, text))

        all_chunks.sort(key=lambda x: x[0])
        full_text = "\n".join(t for _, t in all_chunks)

        # Also check local .tmp for potentially more complete data
        local_path = TMP_DIR / f"g{group_number}_l{lecture_number}_{ctype}.txt"
        if local_path.exists():
            local_text = local_path.read_text(encoding="utf-8")
            if len(local_text) > len(full_text):
                full_text = local_text
                logger.debug("Using local file for %s (larger)", ctype)

        if full_text.strip():
            results[ctype] = full_text

            # Save merged data
            MERGED_DIR.mkdir(parents=True, exist_ok=True)
            out = MERGED_DIR / f"g{group_number}_l{lecture_number}_{ctype}.txt"
            out.write_text(full_text, encoding="utf-8")

        logger.info(
            "Extracted %s for G%d L%d: %d chars (%d vectors)",
            ctype,
            group_number,
            lecture_number,
            len(full_text),
            len(ids),
        )

    return results


# ---------------------------------------------------------------------------
# Step 2: Entity extraction via Gemini
# ---------------------------------------------------------------------------


def extract_entities(
    group_number: int,
    lecture_number: int,
    content: dict[str, str],
) -> dict[str, Any]:
    """Extract entities via Claude Sonnet 4.6 (Gemini free-tier 20/day was insufficient for bulk vault rebuild).

    Args:
        group_number: Training group (1 or 2).
        lecture_number: Lecture sequence number.
        content: Dict of content_type -> text.

    Returns:
        Parsed JSON dict with concepts, relationships, etc.
    """
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("No Anthropic API key configured for entity extraction")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Combine summary + deep_analysis for richest extraction.
    # Cap at 30K each so prompt+output fits comfortably under Sonnet's response budget.
    texts = []
    for ctype in ("summary", "deep_analysis"):
        if ctype in content:
            texts.append(content[ctype][:30000])

    if not texts:
        # Fall back to transcript
        if "transcript" in content:
            texts.append(content["transcript"][:30000])

    if not texts:
        logger.warning(
            "No content available for entity extraction (G%d L%d)",
            group_number,
            lecture_number,
        )
        return {}

    combined = "\n\n---\n\n".join(texts)
    prompt = ENTITY_EXTRACTION_PROMPT + combined

    # Sonnet 4.6: more reliable JSON output than Haiku (no truncation observed in testing).
    # max_tokens=16000 gives headroom for 30 concepts + relationships + examples.
    _ENTITY_MODEL = "claude-sonnet-4-6"

    def _do_extract() -> dict[str, Any]:
        response = client.messages.create(
            model=_ENTITY_MODEL,
            max_tokens=16000,
            timeout=300.0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if "```" in text:
                text = text[: text.rfind("```")]
        text = text.strip()
        return json.loads(text)

    data = retry_with_backoff(
        _do_extract,
        max_retries=5,
        backoff_base=10.0,
        operation_name="entity extraction (Claude Sonnet)",
    )

    # Post-extraction validation
    data = _validate_entities(data)

    # Save to disk
    ENTITIES_DIR.mkdir(parents=True, exist_ok=True)
    key = f"g{group_number}_l{lecture_number}"
    entity_path = ENTITIES_DIR / f"{key}.json"
    entity_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    nc = len(data.get("concepts", []))
    nr = len(data.get("relationships", []))
    logger.info(
        "Extracted entities for G%d L%d: %d concepts, %d relationships",
        group_number,
        lecture_number,
        nc,
        nr,
    )
    return data


# ---------------------------------------------------------------------------
# Step 3: Build concept index across all lectures
# ---------------------------------------------------------------------------


def _build_concept_index(all_entities: dict[str, dict]) -> dict[str, dict]:
    """Build a cross-lecture concept index from all entity data.

    Uses normalized names as keys for deduplication. Stores the "best"
    display name (longest variant seen) for each normalized key.
    """
    # norm_key -> {display_name, ...data}
    concept_index: dict[str, dict] = {}
    # Track display name candidates: norm_key -> list of raw names
    name_variants: dict[str, list[str]] = defaultdict(list)

    def _get_or_create(name: str) -> dict:
        norm = _normalize_concept_name(name)
        name_variants[norm].append(name)
        if norm not in concept_index:
            concept_index[norm] = {
                "display_name": _get_display_name(name),
                "descriptions": [],
                "lectures": [],
                "category": "concept",
                "name_ka": "",
                "aliases": set(),
                "relationships": [],
                "practical_uses": [],
            }
        return concept_index[norm]

    for lecture_key, data in all_entities.items():
        g, lec = _parse_lecture_key(lecture_key)

        for concept in data.get("concepts", []):
            name = concept.get("name", "").strip()
            if not name or len(name) < 3:
                continue
            c = _get_or_create(name)
            c["lectures"].append((g, lec))
            if concept.get("description"):
                c["descriptions"].append(concept["description"])
            if concept.get("category"):
                c["category"] = concept["category"]
            if concept.get("name_ka"):
                c["name_ka"] = concept["name_ka"]
                if concept["name_ka"] != name:
                    c["aliases"].add(concept["name_ka"])

        for rel in data.get("relationships", []):
            from_name = rel.get("from", "").strip()
            to_name = rel.get("to", "").strip()
            rel_type = rel.get("type", "related")
            if from_name and to_name:
                _get_or_create(from_name)["relationships"].append(
                    {"target": to_name, "type": rel_type, "group": g, "lecture": lec}
                )
                _get_or_create(to_name)["relationships"].append(
                    {"target": from_name, "type": rel_type, "group": g, "lecture": lec}
                )

        for example in data.get("practical_examples", []):
            tool = example.get("tool", "").strip()
            use_case = example.get("use_case", "")
            if tool:
                _get_or_create(tool)["practical_uses"].append(
                    {"use_case": use_case, "group": g, "lecture": lec}
                )

    # Pick the best display name for each normalized key
    # Prefer: canonical name (if mapped), then most frequent variant, then longest
    raw_count = sum(len(v) for v in name_variants.values())
    for norm_key, variants in name_variants.items():
        if norm_key in concept_index:
            # Check if any variant has a canonical mapping
            canonical = None
            for v in variants:
                mapped = _get_display_name(v)
                if mapped != v.strip():
                    canonical = mapped
                    break
            if canonical:
                best = canonical
            else:
                counts = Counter(variants)
                best = max(counts, key=lambda v: (counts[v], len(v)))
            concept_index[norm_key]["display_name"] = best
            # Add non-best variants as aliases
            for v in set(variants):
                if v != best:
                    concept_index[norm_key]["aliases"].add(v)

    logger.info(
        "Normalized %d raw concept references into %d unique entries",
        raw_count, len(concept_index),
    )
    return concept_index


# ---------------------------------------------------------------------------
# Step 4: Generate Obsidian vault files
# ---------------------------------------------------------------------------


def _ensure_vault_dirs() -> None:
    """Create the vault directory structure."""
    dirs = [
        VAULT_ROOT,
        VAULT_ROOT / "ლექციები",
        VAULT_ROOT / "ლექციები" / "ჯგუფი 1",
        VAULT_ROOT / "ლექციები" / "ჯგუფი 2",
        VAULT_ROOT / "კონცეფციები",
        VAULT_ROOT / "ინსტრუმენტები",
        VAULT_ROOT / "პრაქტიკული მაგალითები",
        VAULT_ROOT / "WhatsApp დისკუსიები",
        VAULT_ROOT / "ანალიზი",
        VAULT_ROOT / "ანალიზი" / "ჯგუფი 1",
        VAULT_ROOT / "ანალიზი" / "ჯგუფი 2",
        VAULT_ROOT / ".obsidian",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _generate_lecture_note(
    g: int,
    lec: int,
    entity_data: dict[str, Any],
) -> str:
    """Generate a single lecture markdown note."""
    date = _compute_lecture_date(g, lec)
    title = entity_data.get("lecture_title", f"ლექცია #{lec}")

    summary_path = MERGED_DIR / f"g{g}_l{lec}_summary.txt"
    transcript_path = MERGED_DIR / f"g{g}_l{lec}_transcript.txt"
    summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    transcript = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""

    concepts = entity_data.get("concepts", [])
    examples = entity_data.get("practical_examples", [])
    key_points = entity_data.get("key_points", [])
    relationships = entity_data.get("relationships", [])

    concept_by_cat: dict[str, list] = defaultdict(list)
    for c in concepts:
        concept_by_cat[c.get("category", "concept")].append(c)

    prev_link = f"[[ლექცია {lec - 1}]]" if lec > 1 else "---"
    next_link = f"[[ლექცია {lec + 1}]]" if lec < 15 else "---"

    related = set()
    for r in relationships:
        related.add(r.get("from", ""))
        related.add(r.get("to", ""))
    related.discard("")

    note = f"""---
tags: [ლექცია, ჯგუფი-{g}]
date: {date}
group: {g}
lecture: {lec}
---

# {title}

> ჯგუფი #{g} -- ლექცია #{lec}
> თარიღი: {date}

---

## შეჯამება

{summary[:8000] if summary else "_შეჯამება ხელმისაწვდომი არ არის_"}

---

## ძირითადი თემები

"""
    if key_points:
        for kp in key_points[:15]:
            note += f"- {kp}\n"

    cat_names = {
        "tool": "ინსტრუმენტები",
        "platform": "პლატფორმები",
        "concept": "კონცეფციები",
        "technique": "ტექნიკები",
        "methodology": "მეთოდოლოგიები",
    }
    for cat, cat_name in cat_names.items():
        items = concept_by_cat.get(cat, [])
        if items:
            note += f"\n### {cat_name}\n"
            for c in items:
                desc = c.get("description", "")
                name_ka = c.get("name_ka", "")
                display = _wikilink(c["name"])
                if name_ka and name_ka != c["name"]:
                    display += f" ({name_ka})"
                if desc:
                    display += f" -- {desc[:120]}"
                note += f"- {display}\n"

    if examples:
        note += "\n---\n\n## პრაქტიკული მაგალითები\n\n"
        for ex in examples:
            note += f"- {_wikilink(ex.get('tool', ''))} -- {ex.get('use_case', '')}\n"

    note += f"""
---

## კავშირები

- წინა: {prev_link}
- შემდეგი: {next_link}
- დაკავშირებული: {', '.join(_wikilink(c) for c in sorted(related)[:10])}

---

## სრული ტრანსკრიფცია

> [!note]- ტრანსკრიფციის გახსნა (დააჭირეთ გასახსნელად)
"""
    if transcript:
        for line in transcript[:50000].split("\n"):
            note += f"> {line}\n"
    else:
        note += "> _ტრანსკრიფცია ხელმისაწვდომი არ არის_\n"

    return note


def _generate_analysis_note(g: int, lec: int) -> str | None:
    """Generate analysis note from gap + deep analysis files."""
    gap_path = MERGED_DIR / f"g{g}_l{lec}_gap_analysis.txt"
    deep_path = MERGED_DIR / f"g{g}_l{lec}_deep_analysis.txt"

    gap = gap_path.read_text(encoding="utf-8") if gap_path.exists() else ""
    deep = deep_path.read_text(encoding="utf-8") if deep_path.exists() else ""

    if not gap and not deep:
        return None

    date = _compute_lecture_date(g, lec)

    return f"""---
tags: [ანალიზი, ჯგუფი-{g}]
date: {date}
group: {g}
lecture: {lec}
---

# ანალიზი -- ლექცია #{lec} (ჯგუფი #{g})

> დაკავშირებული ლექცია: [[ლექცია {lec}]]

---

## Gap Analysis

{gap[:30000] if gap else "_არ არის ხელმისაწვდომი_"}

---

## Deep Analysis

{deep[:30000] if deep else "_არ არის ხელმისაწვდომი_"}
"""


def _generate_concept_note(name: str, info: dict) -> str:
    """Generate a single concept or tool note."""
    category = info.get("category", "concept")
    tag_cat = "ინსტრუმენტი" if category in TOOL_CATEGORIES else "კონცეფცია"

    aliases = info.get("aliases", set())
    name_ka = info.get("name_ka", "")
    descriptions = info.get("descriptions", [])
    lectures = info.get("lectures", [])
    relationships = info.get("relationships", [])
    practical_uses = info.get("practical_uses", [])

    alias_list = sorted(aliases)
    if name_ka and name_ka != name and name_ka not in alias_list:
        alias_list.insert(0, name_ka)
    alias_yaml = ", ".join(alias_list)

    best_desc = max(descriptions, key=len) if descriptions else ""

    lecture_lines = [
        f"- [[ლექცია {lec}]] (ჯგუფი {g})" for g, lec in sorted(set(lectures))
    ]

    rel_targets: set[str] = set()
    rel_lines: list[str] = []
    for rel in relationships:
        target = rel["target"]
        if target not in rel_targets and target != name:
            rel_targets.add(target)
            rel_lines.append(f"- {_wikilink(target)}")

    note = f"""---
tags: [{tag_cat}, AI]
aliases: [{alias_yaml}]
category: {category}
---

# {name}

"""
    if name_ka and name_ka != name:
        note += f"> ქართულად: **{name_ka}**\n\n"

    note += f"""## აღწერა

{best_desc if best_desc else f"_{name} -- AI კურსში განხილული {tag_cat}_"}

---

## ლექციებში

{chr(10).join(lecture_lines) if lecture_lines else "- _ჯერ არ არის განხილული_"}

---

## დაკავშირებული

{chr(10).join(rel_lines[:15]) if rel_lines else "- _კავშირები არ არის_"}
"""

    if practical_uses:
        use_lines = [
            f"- {u['use_case']} (ჯგუფი {u['group']}, ლექცია {u['lecture']})"
            for u in practical_uses
        ]
        note += f"""
---

## პრაქტიკული გამოყენება

{chr(10).join(use_lines)}
"""

    return note


def _generate_moc(all_entities: dict, concept_index: dict) -> str:
    """Generate the Map of Content (MOC) note."""
    # Identify AI models vs other tools
    ai_kw = [
        "claude", "chatgpt", "gemini", "grok", "perplexity", "gpt",
        "llama", "mistral", "deepseek", "openai", "anthropic",
    ]

    ai_models: list[str] = []
    other_tools: list[str] = []
    for norm_key, info in concept_index.items():
        display = info.get("display_name", norm_key)
        cat = info.get("category", "concept")
        if cat in TOOL_CATEGORIES:
            if any(kw in norm_key for kw in ai_kw):
                ai_models.append(display)
            else:
                other_tools.append(display)
    ai_models.sort()
    other_tools.sort()

    # Cross-lecture concepts
    multi = sorted(
        info.get("display_name", n)
        for n, info in concept_index.items()
        if len(set(info["lectures"])) >= 2
        and info.get("category", "concept") not in TOOL_CATEGORIES
    )

    moc = """---
tags: [MOC, ინდექსი]
---

# AI კურსი -- ცოდნის რუკა

> 15-ლექციანი AI კურსი ქართველი პროფესიონალებისთვის
> 2 ჯგუფი | სამშაბათი/პარასკევი და ორშაბათი/ხუთშაბათი

---

## ლექციები

### ჯგუფი 1 -- მარტის ჯგუფი #1

| # | ლექცია | თარიღი | თემა |
|---|--------|--------|------|
"""
    for grp in [1, 2]:
        if grp == 2:
            moc += """
### ჯგუფი 2 -- მარტის ჯგუფი #2

| # | ლექცია | თარიღი | თემა |
|---|--------|--------|------|
"""
        for lec in range(1, 16):
            key = f"g{grp}_l{lec}"
            date = _compute_lecture_date(grp, lec)
            if key in all_entities:
                title = all_entities[key].get("lecture_title", "")[:60]
                moc += f"| {lec} | [[ლექცია {lec}]] | {date} | {title} |\n"
            else:
                moc += f"| {lec} | ლექცია {lec} | -- | _მოლოდინში_ |\n"

    moc += "\n---\n\n## AI მოდელები\n\n"
    for m in ai_models:
        moc += f"- {_wikilink(m)}\n"

    moc += "\n---\n\n## ინსტრუმენტები და პლატფორმები\n\n"
    for t in other_tools[:30]:
        moc += f"- {_wikilink(t)}\n"

    moc += "\n---\n\n## ძირითადი კონცეფციები (რამდენიმე ლექციაში განხილული)\n\n"
    for c in multi[:30]:
        norm = _normalize_concept_name(c)
        info = concept_index.get(norm, {})
        ls = ", ".join(f"G{g}L{lec}" for g, lec in sorted(set(info.get("lectures", []))))
        moc += f"- {_wikilink(c)} ({ls})\n"

    # Determine progress
    g1_count = sum(1 for k in all_entities if k.startswith("g1_"))
    g2_count = sum(1 for k in all_entities if k.startswith("g2_"))

    moc += f"""
---

## პროგრესი

| ჯგუფი | ლექციები | სტატუსი |
|-------|----------|---------|
| #1 | {g1_count}/15 | მიმდინარე |
| #2 | {g2_count}/15 | მიმდინარე |

---

## ანალიზი

"""
    for grp in [1, 2]:
        moc += f"### ჯგუფი {grp}\n"
        for lec in range(1, 16):
            key = f"g{grp}_l{lec}"
            if key in all_entities:
                moc += f"- [[ლექცია {lec} -- ანალიზი]]\n"
        moc += "\n"

    moc += """---

## პრაქტიკული მაგალითები

- [[ინდექსი|პრაქტიკული მაგალითების ინდექსი]]

## WhatsApp დისკუსიები

- [[ინდექსი|WhatsApp ჩატის ისტორია]]

---

> ეს ცოდნის ბაზა ავტომატურად განახლდება ყოველი ახალი ლექციის შემდეგ.
> სინქრონიზაცია: `python -m tools.integrations.obsidian_sync`
"""
    return moc


def _generate_obsidian_config() -> None:
    """Write .obsidian config files for graph view, app settings, etc."""
    graph_config = {
        "collapse-filter": False,
        "search": "",
        "showTags": True,
        "showAttachments": False,
        "hideUnresolved": False,
        "showOrphans": True,
        "collapse-color-groups": False,
        "colorGroups": [
            {"query": "tag:#ლექცია", "color": {"a": 1, "hex": "#5588ff"}},
            {"query": "tag:#ინსტრუმენტი", "color": {"a": 1, "hex": "#ff8855"}},
            {"query": "tag:#კონცეფცია", "color": {"a": 1, "hex": "#55cc55"}},
            {"query": "tag:#ანალიზი", "color": {"a": 1, "hex": "#cc55cc"}},
            {"query": "tag:#MOC", "color": {"a": 1, "hex": "#ffcc00"}},
        ],
        "collapse-display": False,
        "showArrow": True,
        "textFadeMultiplier": 0,
        "nodeSizeMultiplier": 1,
        "lineSizeMultiplier": 1,
        "collapse-forces": False,
        "centerStrength": 0.518713248970312,
        "repelStrength": 10,
        "linkStrength": 1,
        "linkDistance": 250,
        "scale": 1,
        "close": False,
    }
    (VAULT_ROOT / ".obsidian" / "graph.json").write_text(
        json.dumps(graph_config, indent=2), encoding="utf-8"
    )

    app_config = {
        "alwaysUpdateLinks": True,
        "newLinkFormat": "shortest",
        "useMarkdownLinks": False,
        "showLineNumber": True,
        "defaultViewMode": "preview",
    }
    (VAULT_ROOT / ".obsidian" / "app.json").write_text(
        json.dumps(app_config, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Stale file cleanup
# ---------------------------------------------------------------------------


def _cleanup_stale_files(concept_index: dict[str, dict]) -> int:
    """Remove vault files that are no longer in the concept index.

    Scans both კონცეფციები and ინსტრუმენტები folders for .md files
    that don't correspond to any current concept.

    Returns:
        Count of deleted files.
    """
    # Build set of expected filenames from current index
    expected_files: set[str] = set()
    for norm_key, info in concept_index.items():
        display = info.get("display_name", norm_key)
        expected_files.add(_safe_filename(display) + ".md")

    deleted = 0
    for folder_name in ("კონცეფციები", "ინსტრუმენტები"):
        folder = VAULT_ROOT / folder_name
        if not folder.exists():
            continue
        for md_file in folder.glob("*.md"):
            if md_file.name not in expected_files:
                md_file.unlink()
                deleted += 1

    if deleted:
        logger.info("Cleaned up %d stale vault files", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_lecture(group_number: int, lecture_number: int) -> dict[str, int]:
    """Sync a single lecture to the Obsidian vault.

    Called after transcribe_lecture pipeline completes. Extracts data from
    Pinecone, runs entity extraction, and generates/updates all relevant
    vault files including the MOC.

    Args:
        group_number: Training group (1 or 2).
        lecture_number: Lecture sequence number.

    Returns:
        Dict with counts: concepts, relationships, files_updated.
    """
    logger.info(
        "Syncing Obsidian vault for Group %d, Lecture %d...",
        group_number,
        lecture_number,
    )

    _ensure_vault_dirs()

    # Step 1: Extract from Pinecone
    content = extract_from_pinecone(group_number, lecture_number)
    if not content:
        logger.warning("No content found in Pinecone for G%d L%d", group_number, lecture_number)
        return {"concepts": 0, "relationships": 0, "files_updated": 0}

    # Step 2: Entity extraction
    entity_data = extract_entities(group_number, lecture_number, content)

    # Step 3: Load all existing entity files for full rebuild
    all_entities = {}
    for f in ENTITIES_DIR.glob("g*_l*.json"):
        key = f.stem
        with open(f, encoding="utf-8") as fh:
            all_entities[key] = json.load(fh)

    # Step 4: Build concept index
    concept_index = _build_concept_index(all_entities)

    # Step 5: Generate files
    files_updated = 0

    # Lecture note
    note = _generate_lecture_note(group_number, lecture_number, entity_data)
    path = VAULT_ROOT / "ლექციები" / f"ჯგუფი {group_number}" / f"ლექცია {lecture_number}.md"
    path.write_text(note, encoding="utf-8")
    files_updated += 1

    # Analysis note
    analysis = _generate_analysis_note(group_number, lecture_number)
    if analysis:
        path = (
            VAULT_ROOT / "ანალიზი" / f"ჯგუფი {group_number}"
            / f"ლექცია {lecture_number} -- ანალიზი.md"
        )
        path.write_text(analysis, encoding="utf-8")
        files_updated += 1

    # Concept/tool notes — filter low-importance, then generate
    pre_filter = len(concept_index)
    filtered_index: dict[str, dict] = {}
    for norm_key, info in concept_index.items():
        display = info.get("display_name", norm_key)
        # Must have a meaningful name
        if not display.strip() or len(display) < 3:
            continue
        # Must have at least 1 description with 10+ chars
        if not any(len(d) >= 10 for d in info.get("descriptions", [])):
            continue
        # Single lecture-group combo: keep only if has relationships
        unique_lectures = set(info.get("lectures", []))
        if len(unique_lectures) <= 1 and not info.get("relationships"):
            continue
        filtered_index[norm_key] = info

    skipped = pre_filter - len(filtered_index)
    if skipped:
        logger.info("Filtered %d low-importance concepts before file generation", skipped)

    for norm_key, info in filtered_index.items():
        display = info["display_name"]
        note_text = _generate_concept_note(display, info)
        category = info.get("category", "concept")
        folder = (
            VAULT_ROOT / "ინსტრუმენტები"
            if category in TOOL_CATEGORIES
            else VAULT_ROOT / "კონცეფციები"
        )
        filename = _safe_filename(display) + ".md"
        (folder / filename).write_text(note_text, encoding="utf-8")
        files_updated += 1

    # Clean up stale files no longer in the index
    _cleanup_stale_files(filtered_index)

    # Practical examples index
    examples_note = "---\ntags: [ინდექსი, პრაქტიკა]\n---\n\n# პრაქტიკული მაგალითები\n\n"
    for lk in sorted(all_entities.keys()):
        g, lec = _parse_lecture_key(lk)
        examples = all_entities[lk].get("practical_examples", [])
        if examples:
            examples_note += f"## ჯგუფი {g}, ლექცია {lec}\n\n"
            for ex in examples:
                examples_note += f"- {_wikilink(ex.get('tool', ''))} -- {ex.get('use_case', '')}\n"
            examples_note += "\n"
    (VAULT_ROOT / "პრაქტიკული მაგალითები" / "ინდექსი.md").write_text(
        examples_note, encoding="utf-8"
    )
    files_updated += 1

    # MOC
    moc = _generate_moc(all_entities, concept_index)
    (VAULT_ROOT / "MOC - ძირითადი თემები.md").write_text(moc, encoding="utf-8")
    files_updated += 1

    # Obsidian config
    _generate_obsidian_config()

    n_concepts = len(entity_data.get("concepts", []))
    n_rels = len(entity_data.get("relationships", []))

    logger.info(
        "Obsidian sync complete: %d concepts, %d relationships, %d files updated",
        n_concepts,
        n_rels,
        files_updated,
    )

    return {
        "concepts": n_concepts,
        "relationships": n_rels,
        "files_updated": files_updated,
    }


def sync_full() -> dict[str, int]:
    """Full vault rebuild from all available Pinecone data.

    Extracts data for all lectures in both groups, runs entity extraction,
    and regenerates the entire vault.

    Returns:
        Dict with total counts.
    """
    logger.info("Starting full Obsidian vault rebuild...")
    _ensure_vault_dirs()

    total = {"concepts": 0, "relationships": 0, "files_updated": 0}

    # Discover which lectures exist in Pinecone
    from tools.integrations.knowledge_indexer import get_pinecone_index

    idx = get_pinecone_index()

    # Check all possible lectures
    existing_lectures: list[tuple[int, int]] = []
    for g in [1, 2]:
        for lec in range(1, 16):
            prefix = f"g{g}_l{lec}_summary_"
            ids = []
            for page in idx.list(prefix=prefix, limit=1):
                ids.extend(page)
            if ids:
                existing_lectures.append((g, lec))
            else:
                # Also check transcript
                prefix = f"g{g}_l{lec}_transcript_"
                for page in idx.list(prefix=prefix, limit=1):
                    ids.extend(page)
                if ids:
                    existing_lectures.append((g, lec))

    logger.info("Found %d lectures in Pinecone: %s", len(existing_lectures), existing_lectures)

    for g, lec in existing_lectures:
        try:
            result = sync_lecture(g, lec)
            for k in total:
                total[k] += result.get(k, 0)
            time.sleep(2)  # Rate limiting for Gemini
        except Exception as e:
            logger.error("Failed to sync G%d L%d: %s", g, lec, e)

    # Generate WhatsApp placeholder
    wa_note = """---
tags: [WhatsApp, დისკუსია]
---

# WhatsApp დისკუსიები

> WhatsApp ჩატის ისტორია ავტომატურად სინქრონიზდება Green API-ით.
> გაუშვით: `python -m tools.integrations.obsidian_sync --whatsapp`

## ჯგუფი 1 -- მარტის ჯგუფი #1
- ჩატის ID: `{g1_id}`

## ჯგუფი 2 -- მარტის ჯგუფი #2
- ჩატის ID: `{g2_id}`
""".format(
        g1_id=WHATSAPP_GROUP1_ID or "not configured",
        g2_id=WHATSAPP_GROUP2_ID or "not configured",
    )
    (VAULT_ROOT / "WhatsApp დისკუსიები" / "ინდექსი.md").write_text(
        wa_note, encoding="utf-8"
    )

    logger.info(
        "Full vault rebuild complete: %d concepts, %d relationships, %d files",
        total["concepts"],
        total["relationships"],
        total["files_updated"],
    )
    return total


def sync_whatsapp() -> int:
    """Sync WhatsApp chat history into the Obsidian vault.

    Fetches recent messages from both group chats and creates/updates
    discussion notes.

    Returns:
        Number of messages synced.
    """
    import httpx

    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        logger.warning("Green API not configured -- cannot sync WhatsApp")
        return 0

    _ensure_vault_dirs()
    total_messages = 0

    chats = [
        (WHATSAPP_GROUP1_ID, 1, "მარტის ჯგუფი #1"),
        (WHATSAPP_GROUP2_ID, 2, "მარტის ჯგუფი #2"),
    ]

    for chat_id, group_num, group_name in chats:
        if not chat_id:
            continue

        url = (
            f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE_ID}"
            f"/getChatHistory/{GREEN_API_TOKEN}"
        )

        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(url, json={"chatId": chat_id, "count": 100})

            if response.status_code != 200:
                logger.warning(
                    "WhatsApp API returned %d for group %d",
                    response.status_code,
                    group_num,
                )
                continue

            messages = response.json()
            if not messages:
                continue

            # Build discussion note
            note = f"""---
tags: [WhatsApp, ჯგუფი-{group_num}]
group: {group_num}
---

# WhatsApp -- {group_name}

> ბოლო {len(messages)} შეტყობინება
> ავტომატურად განახლდა: {time.strftime('%Y-%m-%d %H:%M')}

---

"""
            for msg in reversed(messages):  # Chronological order
                sender = msg.get("senderName", msg.get("senderId", "?"))
                text = msg.get("textMessage", msg.get("caption", ""))
                msg_type = msg.get("typeMessage", "")

                if msg_type == "textMessage" and text:
                    note += f"**{sender}**: {text}\n\n"
                elif msg_type in ("imageMessage", "videoMessage", "documentMessage"):
                    note += f"**{sender}**: _{msg_type}_\n\n"

            filepath = (
                VAULT_ROOT / "WhatsApp დისკუსიები" / f"ჯგუფი {group_num} -- ჩატი.md"
            )
            filepath.write_text(note, encoding="utf-8")
            total_messages += len(messages)
            logger.info("Synced %d WhatsApp messages for group %d", len(messages), group_num)

        except Exception as e:
            logger.error("Failed to sync WhatsApp for group %d: %s", group_num, e)

    return total_messages


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Obsidian vault sync for Training Agent")
    parser.add_argument("--group", "-g", type=int, help="Group number (1 or 2)")
    parser.add_argument("--lecture", "-l", type=int, help="Lecture number (1-15)")
    parser.add_argument("--full", action="store_true", help="Full vault rebuild from Pinecone")
    parser.add_argument("--whatsapp", action="store_true", help="Sync WhatsApp chat history")

    args = parser.parse_args()

    if args.full:
        result = sync_full()
        print(f"\nFull rebuild: {result}")
    elif args.whatsapp:
        n = sync_whatsapp()
        print(f"\nWhatsApp: {n} messages synced")
    elif args.group and args.lecture:
        result = sync_lecture(args.group, args.lecture)
        print(f"\nSync G{args.group} L{args.lecture}: {result}")
    else:
        parser.print_help()
        print("\nExamples:")
        print("  python -m tools.integrations.obsidian_sync --group 1 --lecture 3")
        print("  python -m tools.integrations.obsidian_sync --full")
        print("  python -m tools.integrations.obsidian_sync --whatsapp")
