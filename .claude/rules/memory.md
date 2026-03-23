# Memory & Session Persistence Rules — Training Agent

---

## 1. Session Start Protocol

At the beginning of EVERY session:

1. **Read CLAUDE.md** — understand project context (automatic).
2. **Check `docs/decisions/`** — if it exists, read recent architectural decisions.
3. **Run `git log --oneline -10`** — see what was done recently.
4. **Check for handoff notes** — read `.claude/handoff-*.md` if any exist.
5. **Brief greeting**: "ვხედავ რომ ბოლოს [X]-ზე ვმუშაობდით. გინდათ გააგრძელოთ თუ ახალ რამეზე გადავიდეთ?"

DO NOT recite the entire CLAUDE.md. Acknowledge context silently and get to work.

---

## 2. Session End Protocol

Before the session ends (user says goodbye, context filling up):

1. **Create handoff note**: `.claude/handoff-YYYY-MM-DD.md` (use template below).
2. **Save architectural decisions** to `docs/decisions/` if any were made.
3. **Note user preferences** discovered during session (for auto memory).
4. **Suggest checkpoint**: "გინდათ რომ შენახვის წერტილი შევქმნა სანამ გავჩერდებით?"
5. **Summarize**:
   ```
   დღეს რა გავაკეთეთ:
   - [ნაბიჯი 1]
   - [ნაბიჯი 2]

   ჯერ კიდევ დასამუშავებელია:
   - [დარჩენილი ამოცანა]

   ყველაფერი შენახულია. შემდეგ ჯერზე შეგიძლიათ თქვათ "[suggested prompt]".
   ```

---

## 3. What to Remember Where

| ინფორმაცია | სად შევინახო | რატომ |
|---|---|---|
| არქიტექტურული გადაწყვეტილებები | `docs/decisions/NNN-title.md` | მუდმივი, გაზიარებადი, განხილვადი |
| Build/test ბრძანებები | `CLAUDE.md` | ყოველთვის ხელმისაწვდომი |
| დებაგინგის გადაწყვეტილებები | Auto memory | Claude სწავლობს |
| მომხმარებლის პრეფერენციები | Auto memory | პირადი, ცვალებადი |
| რა აშენდა და რატომ | Git commit messages | ჭეშმარიტების წყარო |
| სესიის კონტექსტი შემდეგ ჯერზე | `.claude/handoff-*.md` | ხიდი სესიებს შორის |
| მნიშვნელოვანი შეცდომები და გადაწყვეტები | Auto memory | მომავალი რეფერენცია |

---

## 4. Architectural Decision Records (ADR)

When a significant technical decision is made, create:

```
docs/decisions/
├── 001-hybrid-n8n-python-architecture.md
├── 002-gemini-claude-analysis-pipeline.md
├── 003-railway-deployment.md
└── ...
```

Each ADR follows this format:
```markdown
# NNN: Decision Title

## Date
YYYY-MM-DD

## Status
accepted / superseded / deprecated

## Context
What problem were we solving?

## Decision
What did we decide?

## Reasoning
Why this choice over alternatives?

## Consequences
What are the trade-offs?
```

Create an ADR when:
- Choosing between n8n-only vs Python implementation for a feature.
- Changing the analysis pipeline (Gemini model, Claude model, prompt strategy).
- Making deployment decisions (Railway config, Docker changes).
- Changing external service integrations (new API, different provider).
- Security-related decisions (auth strategy, webhook validation approach).
- Database or storage decisions (Pinecone config, Drive folder structure).

---

## 5. Context Window Management

### Monitor Usage
- If conversation exceeds 20 exchanges: suggest `/compact`.
- If working on many different things: suggest `/clear` between topics.
- Never let context degrade quality.

### Compact Strategy
- Compact BEFORE reaching 60% context usage.
- When compacting, preserve:
  - Current task state and progress
  - Recent decisions and their reasons
  - Active bugs or issues being worked on
  - Training group details (Group 1: Tue/Fri, Group 2: Mon/Thu)
- When compacting, discard:
  - Early brainstorming that led nowhere
  - Failed attempts that were reverted
  - Verbose API responses already processed
  - Full file contents already summarized

### Tell the user:
"საუბარი გრძელია. შევინახავ შეჯამებას რომ კონტექსტი არ დაიკარგოს. ვერაფერს შეამჩნევთ."

---

## 6. Handoff Note Management

### Keep latest 3 handoff notes only.
- When creating a 4th, suggest deleting the oldest.
- NEVER auto-delete — always ask: "ძველი შენიშვნები წავშალო? (3-ზე მეტია)"

### Handoff note location: `.claude/handoff-YYYY-MM-DD.md`

---

## 7. What Claude Must Never Forget (Across Sessions)

Even across sessions, always remember:
- **Project purpose**: Automated AI training session management for Zoom-based Georgian lectures.
- **Two groups**: Group 1 (Tue/Fri), Group 2 (Mon/Thu), 20:00-22:00 GMT+4, 15 lectures each.
- **Architecture**: Hybrid n8n + Python. n8n orchestrates, Python executes heavy tasks.
- **User doesn't write code**: communicate in plain Georgian, no jargon.
- **Security**: WEBHOOK_SECRET on all endpoints, Zoom HMAC on /zoom-webhook.
- **Testing mandatory**: pytest, mock all external services.
- **Georgian text**: UTF-8 everywhere, prompts stay in Georgian.

These reload from CLAUDE.md and rules every session automatically.

---

## 8. The "Where Was I?" Response

When user returns and asks "სად შევჩერდით?" or similar:
1. Check most recent checkpoint commits.
2. Check handoff notes in `.claude/`.
3. Check auto memory for session notes.
4. Summarize: "ბოლო სესიაში [X] გავაკეთეთ. პროექტი [status]. შემდეგი ნაბიჯი იქნებოდა [Y]."
5. Ask: "ამაზე გავაგრძელოთ?"
