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
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from tools.core.config import (
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
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

ENTITY_EXTRACTION_PROMPT = """Analyze this AI course lecture content (summary + deep analysis) and extract a knowledge graph.
The content is in Georgian. Return ONLY valid JSON (no markdown code blocks) with this structure:
{
  "lecture_title": "lecture title in Georgian",
  "date": "estimated date if mentioned, or empty string",
  "concepts": [
    {"name": "concept name (use English for tech terms, Georgian for general concepts)", "name_ka": "Georgian name if applicable", "description": "brief description in Georgian", "category": "concept|tool|technique|platform|methodology"}
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

IMPORTANT:
- Use English names for tech tools/concepts (Claude, ChatGPT, n8n, MCP, VS Code, etc.)
- Keep descriptions in Georgian
- Extract ALL concepts, tools, and techniques mentioned
- Include relationships between concepts
- Be thorough - extract everything mentioned

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
    """Use Gemini to extract entities and relationships from lecture content.

    Args:
        group_number: Training group (1 or 2).
        lecture_number: Lecture sequence number.
        content: Dict of content_type -> text.

    Returns:
        Parsed JSON dict with concepts, relationships, etc.
    """
    from google import genai

    api_key = GEMINI_API_KEY_PAID or GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("No Gemini API key configured for entity extraction")

    client = genai.Client(api_key=api_key)

    # Combine summary + deep_analysis for richest extraction
    texts = []
    for ctype in ("summary", "deep_analysis"):
        if ctype in content:
            texts.append(content[ctype][:40000])

    if not texts:
        # Fall back to transcript
        if "transcript" in content:
            texts.append(content["transcript"][:40000])

    if not texts:
        logger.warning(
            "No content available for entity extraction (G%d L%d)",
            group_number,
            lecture_number,
        )
        return {}

    combined = "\n\n---\n\n".join(texts)
    prompt = ENTITY_EXTRACTION_PROMPT + combined

    def _do_extract() -> dict[str, Any]:
        response = client.models.generate_content(
            model=ENTITY_EXTRACTION_MODEL,
            contents=prompt,
        )
        text = response.text.strip()
        # Remove markdown code blocks if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if "```" in text:
                text = text[: text.rfind("```")]
        text = text.strip()
        return json.loads(text)

    data = retry_with_backoff(
        _do_extract,
        max_retries=3,
        backoff_base=5.0,
        operation_name="entity extraction",
    )

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
    """Build a cross-lecture concept index from all entity data."""
    concept_index: dict[str, dict] = {}

    def _get_or_create(name: str) -> dict:
        if name not in concept_index:
            concept_index[name] = {
                "descriptions": [],
                "lectures": [],
                "category": "concept",
                "name_ka": "",
                "aliases": set(),
                "relationships": [],
                "practical_uses": [],
            }
        return concept_index[name]

    for lecture_key, data in all_entities.items():
        g, l = _parse_lecture_key(lecture_key)

        for concept in data.get("concepts", []):
            name = concept.get("name", "").strip()
            if not name or len(name) < 2:
                continue
            c = _get_or_create(name)
            c["lectures"].append((g, l))
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
                    {"target": to_name, "type": rel_type, "group": g, "lecture": l}
                )
                _get_or_create(to_name)["relationships"].append(
                    {"target": from_name, "type": rel_type, "group": g, "lecture": l}
                )

        for example in data.get("practical_examples", []):
            tool = example.get("tool", "").strip()
            use_case = example.get("use_case", "")
            if tool:
                _get_or_create(tool)["practical_uses"].append(
                    {"use_case": use_case, "group": g, "lecture": l}
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
    l: int,
    entity_data: dict[str, Any],
) -> str:
    """Generate a single lecture markdown note."""
    date = _compute_lecture_date(g, l)
    title = entity_data.get("lecture_title", f"ლექცია #{l}")

    summary_path = MERGED_DIR / f"g{g}_l{l}_summary.txt"
    transcript_path = MERGED_DIR / f"g{g}_l{l}_transcript.txt"
    summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    transcript = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""

    concepts = entity_data.get("concepts", [])
    examples = entity_data.get("practical_examples", [])
    key_points = entity_data.get("key_points", [])
    relationships = entity_data.get("relationships", [])

    concept_by_cat: dict[str, list] = defaultdict(list)
    for c in concepts:
        concept_by_cat[c.get("category", "concept")].append(c)

    prev_link = f"[[ლექცია {l - 1}]]" if l > 1 else "---"
    next_link = f"[[ლექცია {l + 1}]]" if l < 15 else "---"

    related = set()
    for r in relationships:
        related.add(r.get("from", ""))
        related.add(r.get("to", ""))
    related.discard("")

    note = f"""---
tags: [ლექცია, ჯგუფი-{g}]
date: {date}
group: {g}
lecture: {l}
---

# {title}

> ჯგუფი #{g} -- ლექცია #{l}
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


def _generate_analysis_note(g: int, l: int) -> str | None:
    """Generate analysis note from gap + deep analysis files."""
    gap_path = MERGED_DIR / f"g{g}_l{l}_gap_analysis.txt"
    deep_path = MERGED_DIR / f"g{g}_l{l}_deep_analysis.txt"

    gap = gap_path.read_text(encoding="utf-8") if gap_path.exists() else ""
    deep = deep_path.read_text(encoding="utf-8") if deep_path.exists() else ""

    if not gap and not deep:
        return None

    date = _compute_lecture_date(g, l)

    return f"""---
tags: [ანალიზი, ჯგუფი-{g}]
date: {date}
group: {g}
lecture: {l}
---

# ანალიზი -- ლექცია #{l} (ჯგუფი #{g})

> დაკავშირებული ლექცია: [[ლექცია {l}]]

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
        f"- [[ლექცია {l}]] (ჯგუფი {g})" for g, l in sorted(set(lectures))
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
    for name, info in concept_index.items():
        cat = info.get("category", "concept")
        if cat in TOOL_CATEGORIES:
            if any(kw in name.lower() for kw in ai_kw):
                ai_models.append(name)
            else:
                other_tools.append(name)
    ai_models.sort()
    other_tools.sort()

    # Cross-lecture concepts
    multi = sorted(
        n
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
        info = concept_index[c]
        ls = ", ".join(f"G{g}L{l}" for g, l in sorted(set(info["lectures"])))
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

    # Concept/tool notes (rebuild all for cross-lecture consistency)
    for name, info in concept_index.items():
        if not name.strip() or len(name) < 2:
            continue
        note_text = _generate_concept_note(name, info)
        category = info.get("category", "concept")
        folder = (
            VAULT_ROOT / "ინსტრუმენტები"
            if category in TOOL_CATEGORIES
            else VAULT_ROOT / "კონცეფციები"
        )
        filename = _safe_filename(name) + ".md"
        (folder / filename).write_text(note_text, encoding="utf-8")
        files_updated += 1

    # Practical examples index
    examples_note = "---\ntags: [ინდექსი, პრაქტიკა]\n---\n\n# პრაქტიკული მაგალითები\n\n"
    for lk in sorted(all_entities.keys()):
        g, l = _parse_lecture_key(lk)
        examples = all_entities[lk].get("practical_examples", [])
        if examples:
            examples_note += f"## ჯგუფი {g}, ლექცია {l}\n\n"
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
        for l in range(1, 16):
            prefix = f"g{g}_l{l}_summary_"
            ids = []
            for page in idx.list(prefix=prefix, limit=1):
                ids.extend(page)
            if ids:
                existing_lectures.append((g, l))
            else:
                # Also check transcript
                prefix = f"g{g}_l{l}_transcript_"
                for page in idx.list(prefix=prefix, limit=1):
                    ids.extend(page)
                if ids:
                    existing_lectures.append((g, l))

    logger.info("Found %d lectures in Pinecone: %s", len(existing_lectures), existing_lectures)

    for g, l in existing_lectures:
        try:
            result = sync_lecture(g, l)
            for k in total:
                total[k] += result.get(k, 0)
            time.sleep(2)  # Rate limiting for Gemini
        except Exception as e:
            logger.error("Failed to sync G%d L%d: %s", g, l, e)

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
                timestamp = msg.get("timestamp", 0)

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
