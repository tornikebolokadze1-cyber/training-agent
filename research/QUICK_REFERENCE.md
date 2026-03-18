# Dashboard Design Quick Reference Guide

One-page visual and conceptual reference for building performance dashboards.

---

## The Golden Rule: 5-9 Metrics Maximum

```
✓ 5-9 metrics     → User can hold full picture
⚠ 10-14 metrics   → User must scan, forget, re-scan
✗ 15+ metrics     → Cognitive overload
```

---

## Information Hierarchy Template

```
┌─────────────────────────────────────────────┐
│ TIER 1: PRIMARY FOCUS                       │  (Large, colored)
│ ┌─────────────────────────────────────────┐ │
│ │  🔥 12-Day Streak                       │ │
│ │  ████████████░░░░░░ 75% Engagement     │ │
│ └─────────────────────────────────────────┘ │
├─────────────────────────────────────────────┤
│ TIER 2: CONTEXT                             │  (Medium, subtle)
│ ┌──────────────────────┬──────────────────┐ │
│ │ 4-Week Trend        │ Weekly Goal      │ │
│ │ (Line chart)        │ 3 of 5 lectures  │ │
│ └──────────────────────┴──────────────────┘ │
├─────────────────────────────────────────────┤
│ TIER 3: DETAILS (Tap to expand)             │  (Small, collapsible)
│ ┌─────────────────────────────────────────┐ │
│ │ Topic Mastery Matrix                    │ │
│ │ Achievements List                       │ │
│ └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

---

## Chart Type Decision Tree

```
What are you visualizing?

1. Trend over time?
   → Line Chart ✓ (easiest to read slope)

2. Comparison vs target?
   → Bullet Chart ✓ (efficient, shows target)

3. How much is complete?
   → Progress Bar ✓ (dopamine trigger)

4. Consistency pattern?
   → Heat Map/Calendar ✓ (visual pattern)

5. Part-to-whole?
   → Pie Chart ✓ (intuitive proportions)

6. Before vs after?
   → Side-by-side Bars ✓ (quick visual)

7. Multiple dimensions?
   → Radar/Spider ✓ (all axes visible)

Never use Gauge Charts unless executive says yes.
Use Bullet Charts instead (research-backed).
```

---

## Color Coding (RAG System)

```
Performance Level        Status      Color      Icon
═════════════════════════════════════════════════════
≥80% (On target)        ✓ Good      🟢 Green   ✓
60-80% (Acceptable)     ⚠ Caution   🟡 Yellow  ⚠️
<60% (Needs action)     ✗ Critical  🔴 Red     ✗

Rules:
- Always add icon/text (not color alone)
- Define clear thresholds per metric
- Only highlight vital measures
- Red = actionable issue, not just low score
```

---

## Psychology Levers (What Motivates)

```
Loss Aversion (Strongest)
├─ 🔥 Streak Counter
│  ("Don't lose your 12-day streak!")
│  Effect: 2.3x more daily engagement once streak >7 days
│
├─ Freeze Feature
│  ("Save your streak for one missed day")
│  Effect: Reduces devastation of broken streaks
│
└─ "X hours until streak expires!"
   Effect: Urgency trigger

Anticipation (Strong)
├─ Progress Bars
│  (Watch bar fill = dopamine hits throughout, not just at end)
│  Effect: Brain processes visuals 60,000x faster than text
│
└─ Micro-milestones
   (Celebrate 25%, 50%, 75%, 90%, 100%)
   Effect: Multiple goal gradient peaks

Achievement (Medium)
├─ Badges & Trophies
│  (Visual proof of accomplishment)
│
├─ Level Progression
│  (Beginner → Intermediate → Advanced)
│  Effect: Competence satisfaction
│
└─ Growth Trajectory
   (Show before/after improvement)
   Effect: Evidence of effectiveness

Social Proof (Light)
├─ "90% of users improve by week 4"
│  (Inspiration, not shame)
│
└─ Leaderboards
   (Only if healthy competition culture)
   Effect: Comparison motivation (use carefully)
```

---

## What to Show vs. Hide

```
SHOW PROMINENTLY:
✓ Current streak (loss aversion)
✓ Weekly engagement % (primary metric)
✓ Progress to next goal (anticipation)
✓ Next micro-milestone countdown
✓ Trend arrow (↗ improving)
✓ 4-week trend line
✓ Green/yellow/red status badge

HIDE BY DEFAULT (drill-down):
- Raw numbers
- Historical details
- Detailed topic breakdowns
- Comparative analytics
- Achievement list (show only recent 3)

NEVER HIDE:
- Performance values (show actual numbers)
- Growth direction (always show trend)
- Next actionable step
```

---

## Notification Rules (Avoid Fatigue)

```
DO SEND:
✓ Lecture reminder (30 min before)
✓ Streak in danger (last 2 hours)
✓ Milestone unlocked (immediate celebration)
✓ Weekly summary (Friday 6 PM)
✓ Specific improvement alert ("↗ Pronunciation +8%")

DON'T SEND:
✗ "You haven't logged in" (shame)
✗ "You're below average" (demoralizing)
✗ Every metric change (fatigue)
✗ Multiple alerts per day (unless critical)
✗ Notifications at night (respect sleep)

Frequency:
- Daily: 1-2 max (streak reminder + improvement alert)
- Weekly: 1 summary (Friday evening)
- Milestone: Immediate (celebrations)
- Urgent: Only when action needed
```

---

## Mobile-First Layout

```
MOBILE (320-640px):
┌─────────────────┐
│ [Streak Card]   │  Full width
├─────────────────┤
│ [Engagement]    │  Full width
├─────────────────┤
│ [Trend]         │  Full width
├─────────────────┤
│ [Milestone]     │  Full width
└─────────────────┘
(Scroll vertically)

TABLET (641-1024px):
┌──────────────┬──────────────┐
│ [Streak]     │ [Trend]      │
├──────────────┼──────────────┤
│ [Engage]     │ [Goal]       │
├──────────────┼──────────────┤
│ [Achieve]    │ [Weekly Avg] │
├──────────────┴──────────────┤
│ [Topic Matrix - spans both] │
└──────────────────────────────┘

DESKTOP (1025px+):
┌────────┬────────────┬────────┐
│Streak  │ Engagement │ Trend  │
├────────┼────────────┼────────┤
│Goal    │ Weekly Avg │ Topics │
├────────┴────────────┴────────┤
│ [Milestone - spans 3]        │
├────────────────────────────┘
│ [Achievement - spans 3]
└────────────────────────────┘
```

---

## Responsive Text Sizing

```
           Mobile  Tablet  Desktop
Title      24px    28px    32px
Subtitle   16px    18px    20px
Body       14px    14px    14px
Label      12px    12px    14px
```

---

## Performance Budgets

```
Core Metrics Load Time:    < 1s
Full Dashboard Load:       < 3s
Interactive (First Input): < 3.8s
Smooth Animations:         60fps
Mobile Performance Score:  > 85
Lighthouse Score:          > 90
```

---

## Accessibility Checklist

```
Color Contrast:
✓ All text: 4.5:1 contrast ratio minimum
✓ Large text: 3:1 minimum
✓ Never rely on color alone

Touch Targets:
✓ All buttons: 44×44px minimum
✓ Spacing between: 8px minimum

Keyboard Navigation:
✓ Tab through all interactive elements
✓ Focus ring visible: 2px outline
✓ Logical tab order top-to-bottom

Screen Reader:
✓ Semantic HTML (button, nav, etc.)
✓ alt text for all icons/images
✓ Labels for form inputs

Dark Mode:
✓ Sufficient contrast in dark theme
✓ No pure black (#000) or white (#FFF)
✓ Prefers-color-scheme media query
```

---

## Common Mistakes & Fixes

```
MISTAKE                          FIX
═════════════════════════════════════════════════
Show 15+ metrics                 Show 5-9, enable drill-down
Use gauge charts                 Use bullet charts instead
Color only (colorblind)          Add icon + text + color
"You're below average"           "Your growth opportunity: X%"
Points system                    Streak counter
Single big goal                  Micro-milestones (3d, 7d, 14d)
Hide weaknesses                  Frame as "growth areas"
Ignore mobile                    Mobile-first design
Celebration for tiny metrics     Celebrate meaningful wins
No comparison views              Add before/after comparison
```

---

## Component Sizing

```
Desktop View:
Card width:           350-400px
Max dashboard width:  1400px
Column gap:           24px
Padding:              32px

Tablet View:
Card width:           100% minus 32px
Columns:              2
Gap:                  20px
Padding:              24px

Mobile View:
Card width:           100% minus 16px
Columns:              1
Padding:              16px
Gap:                  12px
```

---

## Animation Timing

```
Fast (Micro-interactions):     150ms
Base (Standard transitions):   300ms
Slow (Page transitions):       500ms
Celebration:                   2500ms (hold + exit)

Easing Curves:
- Entrance:    ease-out (0.4, 0, 0.2, 1)
- Exit:        ease-in (0.4, 0, 0.6, 1)
- Elastic:     cubic-bezier(0.68, -0.55, 0.265, 1.55)
```

---

## Data Update Strategy

```
Real-Time (< 1 second):
- Post-lecture engagement score
- Assignment submission
- Attendance tracking

Fast (5-10 minutes):
- Topic mastery calculation
- Engagement metric aggregation
- Correlation analysis

Batch (Hourly/Daily):
- Weekly/monthly summaries
- Milestone checks
- Trend calculation
- Notification triggers
```

---

## Testing Critical Paths

```
User Journey 1: "Build a Streak"
Day 1: See streak counter (0 days) → Motivation to attend
Day 3: Unlock 3-day milestone → Celebration
Day 7: Unlock 7-day milestone → Bigger celebration
Day 12: "Streak in danger" alert at 9pm → Urgency
Day 13: Milestone progress: "14-day streak (1 day away)"

User Journey 2: "Improve Weak Area"
1. See Topic Matrix: Pronunciation 62%
2. See: "← 18% to target"
3. Try recommended: Speaking exercise
4. See: Pronunciation improved 65% ↗
5. See: Weekly summary "+3% this week"

User Journey 3: "Track Weekly Progress"
1. See: Weekly Engagement 75%
2. See: Trend (Week 1: 62% → Week 4: 78%)
3. See: Before/After comparison
4. Understand: "I'm improving 4% per week"
```

---

## Metrics to Implement (Priority Order)

```
WEEK 1:
1. Lecture attendance (binary: attended/not)
2. Engagement score (0-100)
3. Weekly engagement average

WEEK 2:
4. Streak counter (current + longest)
5. Monthly goal progress (X of Y lectures)
6. Performance trend (4-week line)

WEEK 3:
7. Topic mastery (% per topic)
8. Micro-milestone tracking
9. Achievement list

WEEK 4:
10. Real-time metric updates
11. Notification system
12. Comparison views
```

---

## Color Variable Reference

```css
Primary:     #3B82F6  (Blue - actions, trends)
Success:     #10B981  (Green - good status)
Warning:     #FBBF24  (Yellow - caution)
Danger:      #EF4444  (Red - critical)
Neutral-900: #111827  (Text primary)
Neutral-600: #4B5563  (Text secondary)
Neutral-100: #F3F4F6  (Background light)
```

---

## One-Minute Explanation for Non-Technical Users

```
"This dashboard shows:
- Your daily streak (don't break it!)
- This week's engagement percentage
- How you're improving trend
- What topics need work

You tap to see details, but the main screen keeps
you focused on what matters most: today's progress.

The colors are simple: green = good, yellow = caution,
red = needs attention.

No notifications unless important (new milestone,
streak about to break, or you hit a goal).

On your phone it's vertical, on desktop it spreads
out horizontally. Works everywhere."
```

---

## Files Reference

| Need | File | Section |
|------|------|---------|
| Platform examples | analytics_dashboard_research.md | Best-in-Class Analysis |
| Code examples | dashboard_implementation_guide.md | Frontend Components |
| Design specs | dashboard_design_specs.md | Component Specifications |
| Psychology | RESEARCH_SUMMARY.md | Psychology Principles |
| Implementation plan | RESEARCH_SUMMARY.md | Implementation Roadmap |

---

**Last Updated:** March 2026
**Quick Reference Version:** 1.0
**Derived from:** 50+ platform analysis, 40+ research sources
