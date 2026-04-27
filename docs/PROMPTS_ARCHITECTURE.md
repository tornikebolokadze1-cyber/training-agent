# Prompts Architecture

Source file: `tools/core/prompts.py`
Consumed by: `tools/integrations/gemini_analyzer.py` (via `tools/core/config.py` re-export)

All prompts are written entirely in Georgian to produce native-quality output from Gemini models. Technical terms (API, AI, etc.) are kept in English within the Georgian text.

---

## Prompt Hierarchy

The prompts form a sequential pipeline applied to each lecture recording:

```
Video Recording
    |
    v
[1] TRANSCRIPTION_PROMPT  (multimodal: video -> Georgian transcript)
    |
    v  (if video is chunked)
[1b] TRANSCRIPTION_CONTINUATION_PROMPT  (chunk N/Total)
    |
    v
    Transcript
    |
    +---> [2] SUMMARIZATION_PROMPT  (transcript -> 5-section summary)
    |
    +---> [3] GAP_ANALYSIS_PROMPT  (transcript -> 6-dimension pedagogical critique)
    |
    +---> [4] DEEP_ANALYSIS_PROMPT  (transcript -> 13-section mega-analysis in 3 parts)
```

Steps 2, 3, and 4 all receive the same transcript as input and run through the Claude reasoning + Gemini Georgian writing pipeline (`_gemini_write_georgian` in `gemini_analyzer.py`).

---

## 1. TRANSCRIPTION_PROMPT

**Purpose:** Multimodal video-to-text transcription. The model watches the full lecture video and produces a detailed Georgian transcript.

**Model:** Gemini 2.5 Flash (multimodal, sees video frames + audio)

**Key requirements encoded in the prompt:**

- Transcribe everything said in the lecture as accurately as possible
- Mark speakers: `ლექტორი` (lecturer), `მონაწილე` (participant), `კითხვა აუდიტორიიდან` (audience question)
- Add time markers every 10-15 minutes: `[00:10]`, `[00:25]`, etc.
- Keep technical terms in English when no Georgian equivalent exists
- Describe visible on-screen content using markers:
  - `[სლაიდი: ...]` -- slide content description
  - `[დემო: ...]` -- demo/demonstration description
- Transcribe on-screen text, code, and diagrams into the transcript
- Output must cover both audio and visual content

---

## 2. TRANSCRIPTION_CONTINUATION_PROMPT

**Purpose:** Handles chunked video transcription when a recording is too large for a single API call.

**Model:** Same as TRANSCRIPTION_PROMPT (Gemini 2.5 Flash multimodal)

**Format string parameters:**
- `{chunk_number}` -- current chunk index (2, 3, ...)
- `{total_chunks}` -- total number of chunks

**Key requirements:**

- Continues from where the previous chunk ended
- Same transcription rules as the main prompt (speakers, visual markers, etc.)
- Time markers continue from the previous chunk's last timestamp (not reset to zero)

---

## 3. SUMMARIZATION_PROMPT

**Purpose:** Converts a full transcript into a structured 5-section summary for participants who missed the lecture.

**Model:** Gemini 3.1 Pro Preview (text-only, Georgian writing)

**Output structure -- 5 mandatory sections:**

| # | Section (Georgian) | Section (English) | Content |
|---|---|---|---|
| 1 | მთავარი თემები | Main themes | What topics were discussed |
| 2 | ძირითადი კონცეფციები | Key concepts | New terms and ideas explained |
| 3 | პრაქტიკული მაგალითები | Practical examples | Demos and examples shown |
| 4 | საკვანძო დასკვნები | Key conclusions | Main takeaways |
| 5 | მოქმედების ნაბიჯები | Action steps | What participants should do next |

**Design note:** The prompt instructs the model to be detailed enough that someone who missed the lecture can understand the core material. The summary is shared with the training group via Google Drive.

**Input format:** The prompt ends with `ტრანსკრიპტი:` followed by the transcript text (including `[სლაიდი: ...]` markers from transcription).

---

## 4. GAP_ANALYSIS_PROMPT

**Purpose:** Pedagogical quality critique of the lecture across 6 dimensions. Sent privately to the lecturer only.

**Model:** Gemini 3.1 Pro Preview (text-only, Georgian writing)

**Output structure -- 6 analysis dimensions:**

| # | Dimension (Georgian) | Dimension (English) | What it evaluates |
|---|---|---|---|
| 1 | სწავლების ხარისხი | Teaching quality | Clarity of explanations, vague or incomplete parts, what could be better |
| 2 | კრიტიკული ხარვეზები | Critical gaps | Missing topics, logical gaps, unanswered questions for participants |
| 3 | ტექნიკური სიზუსტე | Technical accuracy | Inaccuracies, errors, outdated information |
| 4 | პედაგოგიკური რეკომენდაციები | Pedagogical recommendations | Structure improvements, exercises/activities, engagement strategies |
| 5 | ტემპი და დროის მართვა | Pace and timing | Pace too fast/slow, time distribution across topics |
| 6 | მომავალი ლექციისთვის რეკომენდაციები | Next lecture recommendations | Topics needing more depth, material to prepare, methodology changes |

**Tone directive:** `იყავი გულწრფელი, კონსტრუქციული და კონკრეტული` -- be honest, constructive, and specific. The goal is continuous improvement of lecture quality.

---

## 5. DEEP_ANALYSIS_PROMPT

**Purpose:** Comprehensive 13-section mega-analysis combining traditional teaching critique with global AI industry context. Sent privately to the lecturer only.

**Model:** Gemini 3.1 Pro Preview (text-only, Georgian writing)

**Persona:** The model adopts three expert roles simultaneously:
1. AI industry analyst
2. Pedagogy specialist
3. Georgian business context consultant

### Part I -- Teaching Quality (Traditional Analysis) -- Sections 1-6

Identical in structure to GAP_ANALYSIS_PROMPT dimensions 1-6:
1. Teaching quality
2. Critical gaps
3. Technical accuracy
4. Pedagogical recommendations
5. Pace and timing
6. Next lecture recommendations

### Part II -- Global AI Trends Context -- Sections 7-10

| # | Section (Georgian) | Section (English) | What it covers |
|---|---|---|---|
| 7 | გლობალური AI ინდუსტრიის კონტექსტი | Global AI industry context | How lecture material compares to world AI trends; comparison with Andrew Ng/DeepLearning.AI, Google, Microsoft, fast.ai, Coursera standards |
| 8 | ბაზრის რელევანტურობა ქართული კონტექსტისთვის | Market relevance for Georgian context | Relevance for Georgian managers and business professionals; practical applicability for local companies; what local context could be included |
| 9 | კონკურენტული ანალიზი | Competitive analysis | Topics covered by competing AI training programs but missing here; 3-5 specific missing skills; white-space opportunities |
| 10 | კრიტიკული ბრმა წერტილები | Critical blind spots | AI concepts/tools critical for 2025-2026 but fully omitted; risk to participants if uncorrected; priority ranking of blind spots |

### Part III -- Action Plan and Scoring -- Sections 11-13

| # | Section (Georgian) | Section (English) | What it contains |
|---|---|---|---|
| 11 | კონკრეტული გაუმჯობესებები | Concrete improvements | 5-7 action steps for the lecturer before the next lecture; each must be specific, measurable, and achievable within one week |
| 12 | ლექციის შეფასება | Lecture scoring | 5-dimension scoring table at X/10 scale with justification (see below) |
| 13 | სარეკომენდაციო შეტყობინება | Critical recommendation | 2-3 sentences of the most critical, honest feedback the lecturer needs to hear; directive: "don't sell it, don't beautify it -- say it directly" |

### Scoring Table (Section 12)

The scoring table uses a structured markdown table format designed for regex extraction by `tools/services/analytics.py`:

| Dimension (Georgian) | Dimension (English) | Scale |
|---|---|---|
| შინაარსის სიღრმე | Content depth | X/10 |
| პრაქტიკული ღირებულება | Practical value | X/10 |
| მონაწილეების ჩართულობა | Participant engagement | X/10 |
| ტექნიკური სიზუსტე | Technical accuracy | X/10 |
| ბაზრის რელევანტურობა | Market relevance | X/10 |
| საერთო შეფასება | Overall score | X/10 |

Each score must include a 1-2 sentence justification.

**Audience note:** The prompt explicitly states this analysis is sent only to the lecturer, not participants. This enables the honest, critical tone.

---

## Key Design Decisions

### 1. All prompts fully in Georgian
Gemini produces higher quality Georgian text when prompted in Georgian. Translating prompts to English and expecting Georgian output degrades quality.

### 2. Structured output for machine parsing
The scoring table in DEEP_ANALYSIS_PROMPT uses a consistent `X/10` format with pipe-delimited markdown tables, enabling `analytics.py` to extract scores via regex for trend tracking across lectures.

### 3. Tone directive: honest, constructive, specific
The Georgian phrase `გულწრფელი, კონსტრუქციული და კონკრეტული` appears in both GAP_ANALYSIS_PROMPT and DEEP_ANALYSIS_PROMPT. This prevents the model from producing vague praise and ensures actionable feedback.

### 4. Visual content markers in transcription
The `[სლაიდი: ...]` and `[დემო: ...]` markers serve dual purpose:
- They capture visual teaching content that audio-only transcription would miss
- The SUMMARIZATION_PROMPT explicitly references these markers, telling the model to incorporate slide descriptions into the summary

### 5. Separation of public vs private reports
- SUMMARIZATION_PROMPT output goes to the shared Google Drive folder (visible to all participants)
- GAP_ANALYSIS_PROMPT and DEEP_ANALYSIS_PROMPT outputs go to a private Drive folder and are sent only to the lecturer via private WhatsApp

### 6. Chunked transcription continuity
TRANSCRIPTION_CONTINUATION_PROMPT ensures time markers continue from the previous chunk's last timestamp rather than resetting, producing a seamless transcript when chunks are concatenated.

### 7. Multi-expert persona in deep analysis
DEEP_ANALYSIS_PROMPT assigns three simultaneous expert roles to produce analysis that spans pedagogy, global AI industry trends, and Georgian business context -- perspectives a single-role prompt would miss.
