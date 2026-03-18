# Dashboard Design Specifications & Visual Mockups

Complete design guide for personal performance tracking dashboard.

---

## Color Palette

### Primary Colors
```
Blue (#3B82F6)     - Primary actions, positive trends
Orange (#F97316)   - Warnings, streaks, hot status
Green (#10B981)    - Success, above target, improvement
Red (#EF4444)      - Critical alerts, below target
Yellow (#FBBF24)   - Caution, needs attention
Gray (#6B7280)     - Secondary text, disabled states
```

### Background & Neutral
```
Light Background (#F3F4F6)     - Page background
Card Background (#FFFFFF)       - Component backgrounds
Text Primary (#111827)          - Primary text
Text Secondary (#6B7280)        - Secondary text
Border (#E5E7EB)                - Component borders
```

### Semantic Colors (RAG Status)
```
Success:    #10B981 (Green)    - On target or exceeding (≥80%)
Warning:    #FBBF24 (Yellow)   - Within acceptable range (60-80%)
Critical:   #EF4444 (Red)      - Below target (<60%)
```

---

## Typography

### Font Stack
```css
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
```

### Type Scale

| Use Case | Font Size | Font Weight | Line Height |
|----------|-----------|------------|-------------|
| Page Title | 32px | 700 | 40px |
| Section Header | 20px | 600 | 28px |
| Card Label | 14px | 600 | 20px |
| Body Text | 14px | 400 | 20px |
| Small Text | 12px | 400 | 16px |
| Metric Value (Primary) | 48px | 700 | 56px |
| Metric Value (Secondary) | 24px | 600 | 32px |

---

## Component Specifications

### 1. Streak Card

```
┌─────────────────────────────────────┐
│                                     │
│            🔥                       │  [Icon: 64px, centered]
│                                     │
│        12 Days                      │  [Font: 48px Bold, Blue #3B82F6]
│                                     │
│     Current Streak                  │  [Font: 14px Gray, centered]
│                                     │
│ ────────────────────────────────── │
│  Record: 28 days    Freeze: 1      │  [Font: 12px Gray, with icons]
│                                     │
│  ┌───────────────────────────────┐ │
│  │ Mark as Complete ✓           │ │  [Button: Full width, Blue]
│  └───────────────────────────────┘ │
│                                     │
└─────────────────────────────────────┘

Spacing:
- Padding: 24px (all sides)
- Gap between elements: 12px
- Border radius: 8px
- Shadow: 0 1px 3px rgba(0,0,0,0.1)

Responsive:
- Mobile: Full width - 8px padding
- Tablet: Max 400px
- Desktop: 350px fixed width
```

### 2. Progress Bar Card

```
Label: "Weekly Engagement"                [Font: 14px Bold, Gray]
                                          ↗ Right: "75%" [Font: 14px Bold]

████████████░░░░░░░░░░░░░░░░░░░         [Bar: 100% width, 12px height]
                                          Gradient: Blue #3B82F6 to #2563EB

Completed: 2 of 5 Lectures              [Font: 12px Gray, left aligned]
        Almost there! 🎯                 [Font: 12px Green, right aligned]

Container:
- Background: White
- Padding: 20px
- Border radius: 8px
- Border: 1px solid #E5E7EB
- Margin bottom: 16px
```

### 3. Metric Card (Standard Layout)

```
┌──────────────────────────────┐
│ Engagement Score             │  [Label: 14px Semi-bold Gray]
│                              │
│        78                    │  [Value: 48px Bold Blue]
│      ↑ +5                    │  [Change: 16px Green with arrow]
│                              │
│ ▄▄▄▄▄▄▄░░░░░░░░░░░░░        │  [Sparkline: Compact 50px width]
│                              │
│ vs. Prev: 73                 │  [Context: 12px Gray]
└──────────────────────────────┘

Hover State:
- Background: Slight Blue tint (#F0F9FF)
- Box shadow: 0 4px 6px rgba(0,0,0,0.1)
- Cursor: pointer

Status States:
- Good (≥80%):      Green background tint (#F0FDF4)
- Caution (60-80%): Yellow background tint (#FFFBEB)
- Critical (<60%):  Red background tint (#FEF2F2)
```

### 4. Topic Mastery Matrix

```
Topic               | Current | Previous | Trend
──────────────────┼─────────┼──────────┼──────
Pronunciation      |  85%    |   78%    |  ↗ +7%  [Green text]
Grammar             |  62%    |   60%    |  ↗ +2%  [Green text]
Vocabulary         |  91%    |   88%    |  ↗ +3%  [Green text]
Listening          |  48%    |   52%    |  ↘ -4%  [Red text]
Speaking           |  56%    |   50%    |  ↗ +6%  [Green text]
──────────────────┴─────────┴──────────┴──────

Cell Styling:
- Current %: Badge with color (Green/Yellow/Red)
- Previous %: Small text gray
- Trend: Arrow + percentage change, colored per trend direction

Row Height: 56px
Column widths: 30% | 20% | 20% | 30%

Mobile (Stack vertically):
Topic
Current: XX% ↗ Previous: XX% (Inline)
```

### 5. Performance Trend Chart

```
100 │                     ╱─────
    │                 ╱───╱
 75 │             ╱───╱
    │         ╱───╱
 50 │     ╱───╱
    │ ╱───╱
 25 │
    │
  0 └────────────────────── →
    W1    W2    W3    W4

Chart Dimensions:
- Min height: 250px
- Min width: 280px
- Padding: 20px
- Line width: 2px
- Gradient fill opacity: 0.1

Interactive:
- Hover point: Show value tooltip
- Tooltip background: #FFF
- Tooltip border: 1px solid #E5E7EB
- Tooltip shadow: 0 4px 6px rgba(0,0,0,0.1)
```

### 6. Achievement Badge

```
┌──────────────────────────────────┐
│  🎓                              │  [Icon: 32px, left aligned]
│                                  │
│  First Lecture                   │  [Title: 16px Bold]
│  Completed your first lecture    │  [Description: 12px Gray]
│                                  │
│  Earned Mar 10, 2026             │  [Date: 10px Gray, right]
└──────────────────────────────────┘

Locked badge (not earned):
- Background: #F3F4F6
- Opacity: 0.6
- Icon: Dimmed
- Text color: #6B7280

Unlocked badge:
- Background: Gradient #FEF3C7 to #FCD34D
- Border: 1px solid #FBBF24
- Icon: Full opacity
- Text color: #92400E

Animation (when unlocked):
- Pop-in: Scale 0 → 1 over 0.4s
- Celebration: Slight bounce at 0.2s mark
```

### 7. Milestone Celebration Overlay

```
When Achievement Unlocked:

┌────────────────────────────────┐
│                                │
│         🏆                     │  [Icon: 64px, animated bounce]
│                                │
│    7-Day Streak!               │  [Title: 24px Bold, Green]
│   You're a Champion!           │  [Subtitle: 16px, secondary]
│                                │
│    [Share Achievement]         │  [Button: Optional]
│                                │
└────────────────────────────────┘

Timing:
- Appear: 0.4s ease-out
- Hold: 2.5s
- Disappear: 0.3s ease-in
- Sound: Optional 200ms ding

Animation:
- Scale: 0.5 → 1.2 → 1 (bounce effect)
- Icon bounce: -20px → 0px → -5px → 0px
```

### 8. Weekly Summary Card

```
┌─────────────────────────────────┐
│  📊 Weekly Summary              │ [Header: 16px Bold]
│                                 │
│  ✅ Attended: 4 of 5            │ [Font: 14px]
│  📈 Engagement: 87%             │
│  🔥 Current Streak: 12 days     │
│  ⭐ Topics Improved: 3          │
│                                 │
│  🎉 Great week!                 │ [Celebration text: 12px Green bold]
│  Keep up the momentum.          │
│                                 │
│  ┌───────────────────────────┐  │
│  │  See Detailed Report  →   │  │ [Link/Button]
│  └───────────────────────────┘  │
│                                 │
└─────────────────────────────────┘

Styling:
- Background: Gradient #F0F9FF to #E0F2FE
- Border: 1px solid #7DD3FC
- Border-radius: 8px
- Padding: 20px
```

---

## Mobile-First Responsive Breakpoints

### Mobile (320px - 640px)
```
Layout: Single column, full width with 16px padding

Visible on Home Screen:
[Streak Card]        ← Full width
[Engagement Bar]     ← Full width
[Performance Trend]  ← Full width
[Milestone] (if new) ← Full width

Bottom: "Scroll for more" indicator
```

### Tablet (641px - 1024px)
```
Layout: 2-column grid, 20px gap, 24px padding

Grid Layout:
[Streak Card]         [Performance Trend]
[Engagement Bar]      [Weekly Goal]
[Achievement]         [Milestone]
[Topic Mastery]  (Spans 2 columns)
```

### Desktop (1025px+)
```
Layout: 3-column grid, 24px gap, 32px padding

Grid Layout:
[Streak]      [Engagement] [Trend]
[Goal]        [Weekly Avg] [Achievement]
[Milestone]   [Topic Matrix] (spans 2)
```

---

## Animation & Microinteraction Specifications

### Progress Bar Fill Animation
```
Duration: 600ms
Easing: cubic-bezier(0.4, 0, 0.2, 1) [ease-out]

Keyframes:
0%:    width: 0%
50%:   width: previous_width + 2%  [ease-in-out]
100%:  width: new_width

Color pulse at 90%+:
- Green intensity: 100% → 80% → 100%
- Duration: 1000ms, repeats
```

### Streak Danger Pulse
```
Duration: 1000ms
Easing: ease-in-out
Repeating: Yes (every 2 hours if danger condition)

Keyframes:
0%:    scale 1.0, opacity 1.0
50%:   scale 1.05, opacity 0.8
100%:  scale 1.0, opacity 1.0

Color change: Normal blue → Warning orange
```

### Metric Update Bounce
```
When value changes:

Duration: 400ms
Easing: cubic-bezier(0.68, -0.55, 0.265, 1.55) [elastic]

Keyframes:
0%:    scale 1.0, y 0px
25%:   scale 0.95, y -4px
50%:   scale 1.0, y 0px
75%:   scale 1.05, y -2px
100%:  scale 1.0, y 0px
```

### Achievement Unlock Animation
```
Appearance:
- Scale: 0.5 → 1.0 (400ms, ease-out)
- Opacity: 0 → 1 (400ms)
- Slide-in: -20px → 0px from left (400ms)

Icon bounce:
- Duration: 800ms
- Repeat: Once
- Bounces: 3 times with decreasing amplitude

Sound Effect:
- 200ms ding sound (if enabled)
- Volume: 60%
```

---

## Accessibility Specifications

### Color Contrast Ratios (WCAG AA Compliance)
```
- Text on white: 4.5:1 minimum
- Large text on white: 3:1 minimum
- Icons + color indicator: 3:1 minimum + shape/pattern

Testing text combinations:
✓ Dark gray (#111827) on white: 21:1 (AAA)
✓ Blue (#3B82F6) on white: 4.5:1 (AA)
✓ Green (#10B981) on white: 4.5:1 (AA)
✓ Orange (#F97316) on white: 5.5:1 (AA)
✓ Red (#EF4444) on white: 5.7:1 (AA)
```

### Icon + Text Pattern (Color Blind Safe)
```
Do use:
❌ ✗ + Red text           (shape + color)
✅ ✓ + Green text         (shape + color)
📈 ↗ + Green text         (icon + color + text)
📉 ↘ + Red text           (icon + color + text)
🔥 + Orange text          (emoji + color)

Don't use:
✗ Color only with no icon or text
✗ Multiple shades of same color to convey meaning
```

### Touch Target Sizes
```
Minimum touch target: 44px × 44px
- Button: 44px height, 16px+ width padding
- Link: 44px height, padding 8px 12px

Spacing between targets: 8px minimum
```

### Focus States
```
Focus ring:
- Outline: 2px solid #3B82F6
- Outline-offset: 2px
- Border-radius: 4px

Visible on:
- All interactive elements
- Tab-navigable
- Removed on click/touch (visible keyboard only)
```

### Dark Mode Support (Optional)
```css
@media (prefers-color-scheme: dark) {
  --bg-primary: #111827;     /* from #F3F4F6 */
  --bg-card: #1F2937;        /* from #FFFFFF */
  --text-primary: #F3F4F6;   /* from #111827 */
  --text-secondary: #9CA3AF; /* from #6B7280 */
}
```

---

## Performance & Loading States

### Skeleton Loading State
```
┌──────────────────────────────────┐
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │  [Shimmer animation]
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
│                                  │
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
└──────────────────────────────────┘

Shimmer animation:
- Duration: 2000ms
- Gradient: Left-to-right wave
- Loop: Continuous
- Opacity: 0.7 → 1.0 → 0.7
```

### Error State
```
┌──────────────────────────────────┐
│  ⚠️ Unable to Load                │  [Icon: 24px, Red]
│                                  │
│  Please check your connection    │  [Description: 14px Gray]
│  and try again.                  │
│                                  │
│  ┌────────────────────────────┐  │
│  │  Retry                     │  │  [Button: Blue]
│  └────────────────────────────┘  │
└──────────────────────────────────┘
```

### Empty State
```
┌──────────────────────────────────┐
│  🎓                              │  [Icon: 48px, Gray]
│                                  │
│  No Data Yet                     │  [Title: 16px Bold]
│                                  │
│  Attend a lecture to see        │  [Description: 14px Gray]
│  your progress here.            │
│                                  │
│  Next lecture: Tuesday, Mar 19   │  [Info: 12px, secondary]
│  at 8:00 PM                     │
└──────────────────────────────────┘
```

---

## Data Density Options

### Compact View (Mobile, Focus Mode)
```
Show only:
- Streak
- Current engagement %
- Next milestone countdown

Hide:
- Historical data
- Detailed breakdowns
- Secondary metrics
```

### Standard View (Default)
```
Show:
- Streak
- Current engagement + trend
- Weekly/monthly progress
- 3-5 key metrics
- Recent achievements
```

### Detailed View (Desktop, Full Dashboard)
```
Show everything:
- All metrics from standard
- 4-week trend chart
- Topic-by-topic breakdown
- Detailed engagement timeline
- Historical achievements
- Recommendations
```

---

## Responsive Text Display

### Heading Adjustments

| Screen Size | Title | Subtitle | Body Text |
|-------------|-------|----------|-----------|
| Mobile | 24px | 16px | 14px |
| Tablet | 28px | 18px | 14px |
| Desktop | 32px | 20px | 14px |

### Card Content Wrapping

```
Mobile:   100% width (full)
Tablet:   Max 85% width per card
Desktop:  Max 400px per card

Text truncation:
- Single line stats: Truncate at 45 characters + "..."
- Labels: Allow wrap to 2 lines max
- Values: Never truncate
```

---

## Notification Layout

### In-App Notification (Toast)

```
┌─────────────────────────────────────┐
│ 🎉 Milestone Unlocked!              │  [Icon: 24px] [Message: 14px Bold]
│    You earned a 7-Day Streak!       │  [Detail: 12px Gray]
│                                     │  [Auto-dismiss in 5s]
│                                     │  [Swipe to dismiss on mobile]
└─────────────────────────────────────┘

Position: Top right (desktop), Top center (mobile)
Animation: Slide-in from top (300ms)
Dismiss animation: Fade-out (300ms)
```

### Push Notification Preview

```
🔥 Streak in Danger!

Your 12-day streak expires in 2 hours.
Tap to complete today's lecture.

[Complete] [Dismiss]
```

---

## PDF/Print Stylesheet

For generating reports:

```css
@media print {
  /* Hide interactive elements */
  button, input, .interactive { display: none; }

  /* Full-width print layout */
  body { margin: 0; padding: 20mm; }
  .dashboard { page-break-inside: avoid; }
  .card { page-break-inside: avoid; margin-bottom: 10mm; }

  /* High contrast for printing */
  text { color: #000; }
  .bg-light { background: #FFF; }

  /* Print-friendly colors */
  .status-good { background: #E6F4EA; border: 1px solid #34A853; }
  .status-caution { background: #FEF7E0; border: 1px solid #FBBC04; }
  .status-critical { background: #FADBD8; border: 1px solid #EA4335; }
}
```

---

## File Organization for Implementation

```
project-root/
├── components/
│   ├── Dashboard.jsx
│   ├── StreakCard.jsx
│   ├── ProgressBar.jsx
│   ├── MetricCard.jsx
│   ├── TopicMatrix.jsx
│   ├── PerformanceTrend.jsx
│   ├── MilestonesList.jsx
│   └── NotificationToast.jsx
│
├── styles/
│   ├── globals.css
│   ├── components.css
│   ├── animations.css
│   ├── responsive.css
│   └── accessibility.css
│
├── utils/
│   ├── formatting.js
│   ├── colors.js
│   └── animations.js
│
└── hooks/
    ├── useDashboard.js
    ├── useDashboardWebSocket.js
    └── useAnimation.js
```

---

## Browser & Device Support

### Minimum Browser Versions
```
Chrome/Edge:    90+
Firefox:        88+
Safari:         14.1+
iOS Safari:     14.5+
Android Chrome: 90+
```

### Feature Detection
```javascript
// Required features for core functionality
- CSS Grid
- Flexbox
- CSS Variables
- ES6 (async/await)
- WebSocket support

// Progressive enhancement
- CSS Animations (graceful fallback to instant)
- Local Storage (offline support)
- Service Workers (PWA features)
```

---

## Design System Tokens (CSS Variables)

```css
:root {
  /* Colors */
  --color-primary: #3B82F6;
  --color-success: #10B981;
  --color-warning: #FBBF24;
  --color-critical: #EF4444;
  --color-neutral-50: #F9FAFB;
  --color-neutral-100: #F3F4F6;
  --color-neutral-900: #111827;

  /* Spacing */
  --spacing-xs: 4px;
  --spacing-sm: 8px;
  --spacing-md: 16px;
  --spacing-lg: 24px;
  --spacing-xl: 32px;

  /* Typography */
  --font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-size-sm: 12px;
  --font-size-base: 14px;
  --font-size-lg: 16px;
  --font-size-xl: 20px;
  --font-size-2xl: 24px;
  --font-weight-normal: 400;
  --font-weight-semibold: 600;
  --font-weight-bold: 700;

  /* Shadows */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
  --shadow-md: 0 4px 6px rgba(0,0,0,0.1);
  --shadow-lg: 0 10px 15px rgba(0,0,0,0.1);

  /* Border Radius */
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;

  /* Transitions */
  --transition-fast: 150ms ease-in-out;
  --transition-base: 300ms ease-in-out;
  --transition-slow: 500ms ease-in-out;
}
```

---

## Testing Checklist

- [ ] All interactive elements keyboard navigable
- [ ] Color contrast ≥4.5:1 for text
- [ ] Touch targets ≥44×44px
- [ ] Focus states visible and clear
- [ ] Works on screen readers (VoiceOver, NVDA)
- [ ] Mobile responsiveness (375px, 768px, 1024px widths)
- [ ] Animation performance (60fps on mobile)
- [ ] Loading states appear within 200ms
- [ ] Network errors handled gracefully
- [ ] Dark mode (if implemented) tested
- [ ] No layout shift during load (CLS < 0.1)
- [ ] Page interactive within 3.8s (LCP)

---

**Design System Version:** 1.0
**Last Updated:** March 2026
**Framework:** React 18+ + Tailwind CSS 3.3+
