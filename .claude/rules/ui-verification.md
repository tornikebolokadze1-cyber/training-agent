# UI Verification Rules — Training Agent

> This project is primarily backend (FastAPI + Python).
> UI verification applies to: dashboard_preview.html, architecture.html, any future HTML/CSS files.
> Also applies to OpenAPI /docs page (available locally only).

---

## Trigger Conditions

Automatically activate UI verification when ANY of these files change:
- `.html` files (dashboard_preview.html, architecture.html)
- `.css` files (any stylesheets)
- FastAPI response templates (if any Jinja2 templates are added)
- Static assets (images, icons, fonts in the project)
- OpenAPI schema changes that affect `/docs` page

---

## Verification Sequence

### Step 1: Ensure Server is Running
- Check if FastAPI server is running on port 5001.
- If not running, start it: `python -m tools.app.orchestrator` or `uvicorn tools.app.server:app --port 5001`.
- Wait for server ready (check `/health` endpoint).

### Step 2: Navigate to Affected Page
- For HTML files: open the file directly or via local server.
- For API docs: navigate to `http://localhost:5001/docs`.
- Wait for full page load.

### Step 3: Desktop Screenshot (1440x900)
- Use Playwright MCP: `mcp__playwright__browser_resize` to 1440x900.
- Take screenshot with `mcp__playwright__browser_take_screenshot`.
- Evaluate: layout, spacing, alignment, readability.

### Step 4: Mobile Screenshot (375x812)
- Resize to 375x812.
- Check: no horizontal scroll, readable text, usable on small screen.

### Step 5: Tablet Screenshot (768x1024)
- Resize to 768x1024.
- Check: layout adapts, no broken elements.

### Step 6: Accessibility Check
- Run `mcp__playwright__browser_snapshot` for accessibility tree.
- Verify: alt text on images, heading hierarchy, no skipped heading levels.
- Check color contrast (WCAG AA: 4.5:1 for normal text, 3:1 for large text).

### Step 7: Console & Network Check
- `mcp__playwright__browser_console_messages` — zero errors expected.
- `mcp__playwright__browser_network_requests` — no 4xx/5xx responses.
- No broken images, missing CSS/JS, or mixed content warnings.

### Step 8: Present to User
- Show desktop + mobile screenshots.
- Brief plain-language description of what changed.
- Ask: "ნახე შედეგი — კარგად გამოიყურება?"
- Wait for user approval before committing.

---

## Common Checks

### For dashboard_preview.html
- Charts and graphs render correctly.
- Data displays are readable and well-formatted.
- Georgian text renders properly (UTF-8 font support).
- No overlapping elements or broken layout.

### For architecture.html
- Diagram/flowchart is clear and readable.
- Connections between components are visible.
- Labels are not cut off or overlapping.
- Responsive behavior is reasonable (may need horizontal scroll for complex diagrams).

### For /docs (OpenAPI)
- All endpoints listed with correct methods (GET, POST).
- Request/response schemas visible and accurate.
- Try It Out functionality works for public endpoints (/health, /status).
- Georgian characters in descriptions render correctly.

---

## Responsive Design Checks

| Device | Width | Height | Focus |
|---|---|---|---|
| iPhone SE | 375 | 667 | Smallest common phone |
| iPhone 14 | 393 | 852 | Standard modern phone |
| iPad | 768 | 1024 | Tablet portrait |
| Laptop | 1440 | 900 | Standard desktop |

Minimum required: 375, 768, 1440.

### What to Verify at Each Breakpoint
- No horizontal scrollbar (except for wide diagrams).
- Text readable (minimum 14px on mobile).
- Touch targets at least 44x44px on mobile.
- Images scale proportionally.
- Georgian text does not overflow containers.

---

## Performance Checks

| Metric | Target | Critical |
|---|---|---|
| Page Load | < 3s | > 5s |
| No console errors | 0 | > 0 |
| No failed network requests | 0 | > 0 |

---

## Before/After Comparison

When modifying existing HTML/CSS:
1. BEFORE changes: take screenshots at 3 viewports.
2. Make the code changes.
3. AFTER changes: take screenshots at 3 viewports.
4. Compare: intentional vs unintentional differences.
5. Show both to user: "აი როგორ იყო → აი როგორ გახდა."
