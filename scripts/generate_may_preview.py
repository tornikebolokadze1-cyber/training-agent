"""Generate preview artifacts for May 2026 student onboarding messages.

Produces:
  - .tmp/calendar_group3.html  — full schedule for Mon/Thu group
  - .tmp/calendar_group4.html  — full schedule for Tue/Fri group
  - .tmp/personal_message_sample_group3.txt — sample WhatsApp text
  - .tmp/personal_message_sample_group4.txt — sample WhatsApp text

This script ONLY writes files — it does not send anything. User must
visually approve before running ``send_may_personal_messages.py``.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP = PROJECT_ROOT / ".tmp"
TMP.mkdir(exist_ok=True)


def compute_lectures(start: date, weekly_days: list[int], total: int = 15) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < total:
        if d.weekday() in weekly_days:
            out.append(d)
        d += timedelta(days=1)
    return out


GEORGIAN_WEEKDAY = {0: "ორშაბათი", 1: "სამშაბათი", 2: "ოთხშაბათი", 3: "ხუთშაბათი", 4: "პარასკევი", 5: "შაბათი", 6: "კვირა"}
GEORGIAN_MONTH = {
    1: "იანვარი", 2: "თებერვალი", 3: "მარტი", 4: "აპრილი", 5: "მაისი", 6: "ივნისი",
    7: "ივლისი", 8: "აგვისტო", 9: "სექტემბერი", 10: "ოქტომბერი", 11: "ნოემბერი", 12: "დეკემბერი",
}

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian:wght@400;500;600;700;800&family=Noto+Serif+Georgian:wght@600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap');
:root {
  --bg-base: #07070a;
  --ink: #f8f8fb;
  --ink-mute: #9a9aa6;
  --ink-dim: #5a5a64;
  --line: rgba(255,255,255,0.10);
  --line-soft: rgba(255,255,255,0.05);
  --accent: #f5d05b;        /* warm amber */
  --accent-2: #f56565;      /* coral */
  --accent-3: #c084fc;      /* violet */
  color-scheme: dark;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: var(--bg-base); }
body {
  font-family: "Noto Sans Georgian", -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
               "Helvetica Neue", "FiraGO", "Sylfaen", sans-serif;
  width: 800px;
  margin: 0 auto;
  padding: 0;
  line-height: 1.5;
  color: var(--ink);
  background:
    radial-gradient(circle at 20% 12%, rgba(192,132,252,0.32) 0%, transparent 38%),
    radial-gradient(circle at 90% 8%,  rgba(245,208,91,0.22)  0%, transparent 32%),
    radial-gradient(circle at 78% 92%, rgba(245,101,101,0.20) 0%, transparent 36%),
    radial-gradient(circle at 8% 88%,  rgba(34,211,238,0.16)  0%, transparent 32%),
    var(--bg-base);
  font-feature-settings: "ss01";
  -webkit-font-smoothing: antialiased;
}

.poster {
  padding: 60px 56px 56px;
  position: relative;
  min-height: 100vh;
  overflow: hidden;
}
/* subtle film-grain */
.poster::before {
  content: "";
  position: absolute; inset: 0;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 0.06 0'/></filter><rect width='240' height='240' filter='url(%23n)'/></svg>");
  opacity: 0.5; mix-blend-mode: overlay; pointer-events: none;
}

/* Top bar: brand + edition */
.top {
  display: flex; justify-content: space-between; align-items: baseline;
  font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--ink-mute);
  margin-bottom: 56px;
}
.top .brand::before { content: "● "; color: var(--accent); }
.top .edition { font-variant-numeric: tabular-nums; }

/* Title block */
.titlewrap { margin-bottom: 48px; position: relative; padding-right: 40px; }
h1 {
  font-family: "Noto Serif Georgian", Georgia, serif;
  font-weight: 800;
  font-size: 84px;
  line-height: 1.1;
  letter-spacing: -0.03em;
  color: var(--ink);
  margin-bottom: 20px;
  white-space: nowrap;
  overflow: visible;
}
h1 .ai { color: var(--ink); }
h1 .kursi {
  font-style: italic;
  font-weight: 700;
  background: linear-gradient(110deg, var(--accent) 0%, var(--accent-2) 55%, var(--accent-3) 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  padding-left: 14px;
  padding-right: 18px;  /* buffer for italic letter tail */
}
.subtitle {
  font-family: "Noto Sans Georgian", sans-serif;
  font-size: 20px;
  font-weight: 500;
  color: var(--ink-mute);
  margin-top: 8px;
  letter-spacing: -0.005em;
}
.tag {
  display: inline-block;
  margin-top: 22px;
  padding: 10px 18px;
  font-family: "JetBrains Mono", monospace;
  font-size: 11px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--bg-base);
  background: var(--accent);
  border-radius: 4px;
  transform: rotate(-1deg);
  box-shadow: 0 10px 30px -10px rgba(245,208,91,0.5);
}

/* Stat strip */
.stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0;
  margin: 44px 0;
  padding: 22px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
.stat { padding: 0 4px; }
.stat + .stat { border-left: 1px solid var(--line-soft); padding-left: 28px; }
.stat .k {
  font-family: "JetBrains Mono", monospace;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--ink-dim);
  display: block;
}
.stat .v {
  display: block;
  margin-top: 8px;
  font-family: "Noto Sans Georgian", sans-serif;
  font-size: 22px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: -0.02em;
}
.stat .v small {
  font-weight: 500;
  font-size: 14px;
  color: var(--ink-mute);
  margin-left: 4px;
}

/* Schedule grid */
.schedule-label {
  font-family: "JetBrains Mono", monospace;
  font-size: 11px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--ink-dim);
  margin-bottom: 18px;
}
.lecture-list {
  list-style: none;
  display: grid;
  grid-template-columns: 1fr 1fr;
  column-gap: 36px;
  row-gap: 0;
}
.lecture {
  display: grid;
  grid-template-columns: 44px 1fr auto;
  align-items: center;
  gap: 14px;
  padding: 14px 0;
  border-bottom: 1px solid var(--line-soft);
}
.lecture:nth-last-child(-n+2) { border-bottom: 1px solid var(--line); }
.lecture .num {
  font-family: "JetBrains Mono", monospace;
  font-weight: 600;
  font-size: 22px;
  color: var(--ink-dim);
  letter-spacing: -0.04em;
  font-variant-numeric: tabular-nums;
}
.lecture .body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.lecture .date {
  font-family: "Noto Sans Georgian", sans-serif;
  font-size: 17px;
  font-weight: 600;
  color: var(--ink);
  letter-spacing: -0.01em;
  white-space: nowrap;
}
.lecture .day {
  font-family: "JetBrains Mono", monospace;
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink-mute);
}
.lecture .pill {
  font-family: "JetBrains Mono", monospace;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  padding: 4px 8px;
  color: var(--bg-base);
  background: var(--accent);
  border-radius: 999px;
  transform: rotate(2deg);
  white-space: nowrap;
}
.lecture.today .num { color: var(--accent); }
.lecture.today .date { color: var(--accent); }
.lecture.tomorrow .num { color: var(--accent-3); }
.lecture.tomorrow .date { color: var(--accent-3); }
.lecture.tomorrow .pill { background: var(--accent-3); }

/* Footer */
.foot {
  margin-top: 48px;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  padding-top: 28px;
  border-top: 1px solid var(--line);
  font-family: "JetBrains Mono", monospace;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--ink-mute);
}
.foot .sig { color: var(--ink); font-size: 12px; }
.foot .sig::before { content: "→ "; color: var(--accent); }
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ka">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI კურსი — {group_label}</title>
<style>{css}</style>
</head>
<body>
<div class="poster">
  <div class="top">
    <span class="brand">AI Pulse Georgia</span>
    <span class="edition">EDITION · 05 · 2026</span>
  </div>

  <div class="titlewrap">
    <h1><span class="ai">AI</span><span class="kursi">კურსი</span></h1>
    <p class="subtitle">{group_label} · 15 ლექცია</p>
    <span class="tag">{days_text}</span>
  </div>

  <div class="stats">
    <div class="stat"><span class="k">DAYS</span><span class="v">{days_short}</span></div>
    <div class="stat"><span class="k">TIME</span><span class="v">20:00<small>—22:00 GMT+4</small></span></div>
    <div class="stat"><span class="k">FORMAT</span><span class="v">ZOOM<small>· 2h prior</small></span></div>
  </div>

  <p class="schedule-label">— LECTURES · {first_str} → {last_str}</p>
  <ul class="lecture-list">
{rows}
  </ul>

  <div class="foot">
    <span class="sig">მრჩეველი · AI ასისტენტი</span>
    <span>2026 · MAY · COHORT</span>
  </div>
</div>
</body>
</html>
"""


GEORGIAN_WEEKDAY_SHORT = {0: "ორშ", 1: "სამ", 2: "ოთხ", 3: "ხუთ", 4: "პარ", 5: "შაბ", 6: "კვი"}


def render_html(group_label: str, days_text: str, lectures: list[date]) -> str:
    today = date.today()
    rows: list[str] = []
    for i, d in enumerate(lectures, start=1):
        date_str = f"{d.day} {GEORGIAN_MONTH[d.month]} {d.year}"
        day_short = GEORGIAN_WEEKDAY_SHORT[d.weekday()].upper()
        klass = "lecture"
        pill = ""
        if d == today:
            klass += " today"
            pill = '<span class="pill">დღეს</span>'
        elif d == today + timedelta(days=1):
            klass += " tomorrow"
            pill = '<span class="pill">ხვალ</span>'
        rows.append(
            f'    <li class="{klass}">'
            f'<span class="num">{i:02d}</span>'
            f'<div class="body"><span class="date">{date_str}</span>'
            f'<span class="day">{day_short}</span></div>'
            f'{pill or "<span></span>"}'
            '</li>'
        )
    first_str = f"{lectures[0].day} {GEORGIAN_MONTH[lectures[0].month]}".upper()
    last_str = f"{lectures[-1].day} {GEORGIAN_MONTH[lectures[-1].month]} {lectures[-1].year}".upper()
    # Short day-pair: e.g. "ორშ · ხუთ"
    unique_days = sorted({d.weekday() for d in lectures})
    days_short = " · ".join(GEORGIAN_WEEKDAY_SHORT[d] for d in unique_days)
    return HTML_TEMPLATE.format(
        group_label=group_label,
        css=CSS,
        days_text=days_text,
        days_short=days_short,
        first_str=first_str,
        last_str=last_str,
        rows="\n".join(rows),
    )


# Personal WhatsApp message template (Georgian, intro + schedule)
PERSONAL_MSG_TEMPLATE = """გამარჯობა, {first_name}! 👋

მე ვარ „მრჩეველი" — AI ასისტენტი, რომელიც გეხმარებათ AI კურსზე ყველაფერში თორნიკესთან ერთად. ჩემი მთავარი ფუნქცია — WhatsApp საერთო ჯგუფში ვუპასუხო თქვენს კითხვებს კურსის მასალის, ლექციების შინაარსისა და AI ინსტრუმენტების შესახებ. უბრალოდ მომმართეთ ჯგუფში სიტყვით „მრჩეველო" — და მე ვუპასუხებ.

📅 თქვენი ჯგუფი: {group_label}
🗓 ლექციები: {days_text}
🕗 დრო: 20:00 — 22:00 (თბილისის დრო)
📚 სულ: 15 ლექცია
🟢 პირველი ლექცია: {first_human}
🏁 ბოლო ლექცია: {last_human}

🎥 ლექციები: Zoom-ში ჩატარდება, ლინკი ყოველი ლექციიდან 2 საათით ადრე გამოგზავნდება საერთო ჯგუფში.

📂 ჩანაწერები და მოკლე შინაარსი: {drive_url}
(Drive-ის ფოლდერი უკვე გაუზიარდა თქვენს მაილზე — შესვლის შემდეგ ხელმისაწვდომი იქნება ყველა ლექციის ვიდეო და მასალა)

დართულ სურათში ნახავთ მთლიან განრიგს — შეგიძლიათ შეინახოთ ტელეფონში.

წარმატებებს გისურვებთ! თუ რამე გჭირდებათ — მე აქ ვარ. 🚀

—
AI ასისტენტი - მრჩეველი
"""


def render_message(
    first_name: str,
    group_label: str,
    days_text: str,
    lectures: list[date],
    drive_url: str,
) -> str:
    first_human = f"{lectures[0].day} {GEORGIAN_MONTH[lectures[0].month]} {lectures[0].year} ({GEORGIAN_WEEKDAY[lectures[0].weekday()]})"
    last_human = f"{lectures[-1].day} {GEORGIAN_MONTH[lectures[-1].month]} {lectures[-1].year} ({GEORGIAN_WEEKDAY[lectures[-1].weekday()]})"
    return PERSONAL_MSG_TEMPLATE.format(
        first_name=first_name,
        group_label=group_label,
        days_text=days_text,
        first_human=first_human,
        last_human=last_human,
        drive_url=drive_url,
    )


def render_png(html_path: Path, png_path: Path, viewport_width: int = 800) -> None:
    """Render an HTML file to a PNG image using headless Chromium (Playwright)."""
    from playwright.sync_api import sync_playwright

    url = f"file:///{html_path.as_posix()}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": viewport_width, "height": 800},
                device_scale_factor=2,  # retina for crisper image
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle")
            # Wait briefly for webfonts to settle
            page.wait_for_timeout(800)
            page.screenshot(path=str(png_path), full_page=True, type="png", omit_background=False)
        finally:
            browser.close()


def main() -> int:
    g3_lectures = compute_lectures(date(2026, 5, 11), [0, 3])
    g4_lectures = compute_lectures(date(2026, 5, 12), [1, 4])

    # HTML calendars
    g3_html = render_html("მაისის ჯგუფი #1", "ორშაბათი და ხუთშაბათი", g3_lectures)
    g4_html = render_html("მაისის ჯგუფი #2", "სამშაბათი და პარასკევი", g4_lectures)
    g3_html_path = TMP / "calendar_group3.html"
    g4_html_path = TMP / "calendar_group4.html"
    g3_html_path.write_text(g3_html, encoding="utf-8")
    g4_html_path.write_text(g4_html, encoding="utf-8")
    print(f"  Wrote {g3_html_path}")
    print(f"  Wrote {g4_html_path}")

    # PNG render (so WhatsApp shows it inline as image)
    g3_png_path = TMP / "calendar_group3.png"
    g4_png_path = TMP / "calendar_group4.png"
    print("  Rendering PNGs via Playwright...")
    try:
        render_png(g3_html_path, g3_png_path)
        render_png(g4_html_path, g4_png_path)
        print(f"  Wrote {g3_png_path}")
        print(f"  Wrote {g4_png_path}")
    except Exception as e:
        print(f"  [WARN] PNG render failed: {type(e).__name__}: {e}")
        print("  HTML files are still available; you can open them in browser.")

    # Drive folder URLs for each group (lecture folders shared with students)
    g3_drive = "https://drive.google.com/drive/folders/165JQVRq9ueas0wAJhFjHneEtBSvbt_bN"
    g4_drive = "https://drive.google.com/drive/folders/1K4XT7apK7ewI1_ihglb6ob8dWWKo9dOu"

    # Sample messages using real first names from Airtable (already in vocative form)
    g3_sample = render_message("ლევან", "მაისის ჯგუფი #1", "ორშაბათი და ხუთშაბათი", g3_lectures, g3_drive)
    g4_sample = render_message("გურამ", "მაისის ჯგუფი #2", "სამშაბათი და პარასკევი", g4_lectures, g4_drive)
    (TMP / "personal_message_sample_group3.txt").write_text(g3_sample, encoding="utf-8")
    (TMP / "personal_message_sample_group4.txt").write_text(g4_sample, encoding="utf-8")
    print(f"  Wrote {TMP / 'personal_message_sample_group3.txt'}")
    print(f"  Wrote {TMP / 'personal_message_sample_group4.txt'}")

    print()
    print("Open these files to review before sending:")
    print(f"  Group 3 calendar (HTML): file:///{g3_html_path.as_posix()}")
    print(f"  Group 4 calendar (HTML): file:///{g4_html_path.as_posix()}")
    print(f"  Group 3 calendar (PNG):  {g3_png_path}")
    print(f"  Group 4 calendar (PNG):  {g4_png_path}")
    print(f"  Group 3 sample msg: {TMP / 'personal_message_sample_group3.txt'}")
    print(f"  Group 4 sample msg: {TMP / 'personal_message_sample_group4.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
