# PII Redaction Log

**Generated:** 2026-04-24  
**Tool:** `scripts/redact_pii.py`  
**Redaction method:** Regex + whole-word name substitution (originals untouched)

---

## File audit results

### 1. `obsidian-vault/ანალიზი/MID_COURSE_WHATSAPP_PATTERNS.md`

**Status: CONTAINS PII — sanitized copy produced**  
**Safe to commit:** `obsidian-vault/ანალიზი/MID_COURSE_WHATSAPP_PATTERNS.sanitized.md`  
**Original:** keep local only, do NOT commit.

| Category | Count |
|---|---|
| Phone numbers redacted | 30 |
| Student names redacted | 60 |
| Emails redacted | 0 |
| **TOTAL replacements** | **90** |

Name-level breakdown:

| Original name | Code assigned | Occurrences |
|---|---|---|
| Misho (standalone) | student_G2_A | 8 |
| Nikoloz (standalone) | student_G2_B | 8 |
| Shorena | student_G1_A | 7 |
| Lasha | student_G1_H | 4 |
| Maka | student_G1_I | 4 |
| მადონა | student_G2_L | 4 |
| Koba | student_G1_D | 3 |
| Tamar Parunashvili | student_G2_K | 3 |
| TIKO | student_G2_J | 3 |
| ნინო ბეგლარაშვილი | student_G1_B | 2 |
| Lika Lejava | student_G1_C | 2 |
| Misho Laliashvili | student_G2_A | 2 |
| Nikoloz Maisuradze | student_G2_B | 2 |
| Tato🎈🎈🎈 | student_G2_I | 2 |
| Lika (standalone) | student_G1_C | 1 |
| ნინო (standalone) | student_G1_B | 1 |
| beqa chkhubadze | student_G2_C | 1 |
| Neli Kharbedia | student_G2_H | 1 |
| beqa (standalone) | student_G2_C | 1 |
| Tamar (standalone) | student_G2_K | 1 |

Verification: `grep` for all student names in sanitized output → **zero matches**. Digit sequences ≥9 chars → **zero matches**.

---

### 2. `WHATSAPP_DATA_GAP_AUDIT.md`

**Status: CLEAN — no PII found**  
**Safe to commit as-is.**

| Category | Count |
|---|---|
| Phones | 0 |
| Names | 0 |
| Emails | 0 |
| TOTAL | 0 |

The file contains only system architecture, code snippets, timestamps, and aggregate counts. No student identifiers present.

---

### 3. `.claude/handoff-2026-04-24.md`

**Status: CLEAN — no PII found**  
**Safe to commit as-is** (if desired; note it contains internal project notes).

| Category | Count |
|---|---|
| Phones | 0 |
| Names | 0 |
| Emails | 0 |
| TOTAL | 0 |

The file references student codes only in aggregate and does not name individuals.

---

## What is safe to commit to a public repo

| File | Safe? | Notes |
|---|---|---|
| `obsidian-vault/ანალიზი/MID_COURSE_WHATSAPP_PATTERNS.sanitized.md` | ✅ YES | Sanitized copy — all PII replaced with codes |
| `WHATSAPP_DATA_GAP_AUDIT.md` | ✅ YES | No PII found |
| `.claude/handoff-2026-04-24.md` | ✅ YES | No PII found |
| `scripts/redact_pii.py` | ✅ YES | Utility script, no data |
| `obsidian-vault/ანალიზი/MID_COURSE_WHATSAPP_PATTERNS.md` | ❌ NO | Original — 90 PII items; keep local |

## What must stay local (originals with PII)

- `obsidian-vault/ანალიზი/MID_COURSE_WHATSAPP_PATTERNS.md` — 30 phone numbers + 60 student name mentions
- `data/messages.db` — raw WhatsApp archive (already in .gitignore per data/ convention)
- `data/greenapi_backfill/` — raw export JSON (same)
- `obsidian-vault/WhatsApp დისკუსიები/` — raw chat dumps

---

## Redaction tool

```
# Produce a sanitized copy:
python3 -m scripts.redact_pii <input_path> <output_path>

# Check counts without writing:
python3 -m scripts.redact_pii --stats <input_path>
```

Tool is idempotent: running it twice on an already-sanitized file produces zero additional replacements (codes like `student_G2_A` are not in the name list).
