# Dashboard Implementation Guide

Practical code patterns and implementation strategies for personal performance dashboards.

---

## Part 1: Component Architecture

### Recommended Tech Stack for Your Training Dashboard

```
Frontend Layer:
├─ React/Vue.js (component framework)
├─ Recharts or Chart.js (charting library)
├─ Tailwind CSS (styling, responsive design)
└─ Zustand or Pinia (state management)

Backend Layer:
├─ Python FastAPI (metrics calculation)
├─ PostgreSQL (time-series metrics storage)
└─ Redis (real-time metric caching)

Real-time:
└─ WebSocket (push updates to dashboard)
```

---

## Part 2: Core Metrics Data Model

### Database Schema for Performance Tracking

```python
# PostgreSQL tables structure

# Lectures and Attendance
CREATE TABLE lectures (
    id SERIAL PRIMARY KEY,
    date DATE,
    group_id INT,
    topic VARCHAR(255),
    duration_minutes INT,
    recorded_at TIMESTAMP
);

CREATE TABLE attendance (
    id SERIAL PRIMARY KEY,
    lecture_id INT,
    learner_id INT,
    joined_at TIMESTAMP,
    left_at TIMESTAMP,
    duration_minutes INT,
    engagement_score DECIMAL(3,2),  -- 0.0-1.0
    FOREIGN KEY (lecture_id) REFERENCES lectures(id)
);

# Engagement Metrics
CREATE TABLE engagement_metrics (
    id SERIAL PRIMARY KEY,
    learner_id INT,
    lecture_id INT,
    date DATE,

    # Behavioral metrics
    questions_asked INT,
    notes_words INT,
    chat_messages INT,
    screen_time_minutes INT,

    # Performance metrics
    quiz_score DECIMAL(3,2),
    assignment_completion BOOLEAN,

    # Calculated scores
    engagement_score DECIMAL(3,2),
    comprehension_score DECIMAL(3,2),

    FOREIGN KEY (lecture_id) REFERENCES lectures(id),
    UNIQUE(learner_id, lecture_id, date)
);

# Topic Mastery Progress
CREATE TABLE topic_mastery (
    id SERIAL PRIMARY KEY,
    learner_id INT,
    topic_id INT,
    date DATE,
    mastery_score DECIMAL(3,2),  -- 0.0-1.0
    attempts INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

# Streak Tracking
CREATE TABLE streaks (
    id SERIAL PRIMARY KEY,
    learner_id INT,
    streak_type VARCHAR(50),  -- 'attendance', 'assignments', etc.
    current_streak INT,
    longest_streak INT,
    started_at DATE,
    last_activity_date DATE,
    freeze_used BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

# Milestones
CREATE TABLE milestones (
    id SERIAL PRIMARY KEY,
    learner_id INT,
    milestone_type VARCHAR(50),  -- '3_day_streak', 'first_perfect_score', etc.
    earned_at TIMESTAMP,
    archived BOOLEAN DEFAULT FALSE
);
```

---

## Part 3: Backend Metrics Calculation

### FastAPI Endpoint to Fetch Dashboard Data

```python
from fastapi import FastAPI, Depends, HTTPException
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
import asyncio

app = FastAPI()

@app.get("/api/dashboard/{learner_id}")
async def get_dashboard_data(
    learner_id: int,
    db: Session = Depends(get_db)
):
    """
    Fetch comprehensive dashboard data for a learner.
    Returns all metrics needed for home screen render.
    """

    # 1. Current Streak (Loss Aversion)
    current_streak = db.query(Streaks).filter(
        Streaks.learner_id == learner_id,
        Streaks.streak_type == 'lecture_attendance'
    ).first()

    # 2. This Week's Performance
    week_ago = datetime.now() - timedelta(days=7)
    week_metrics = db.query(AttendanceMetrics).filter(
        AttendanceMetrics.learner_id == learner_id,
        AttendanceMetrics.date >= week_ago
    ).all()

    weekly_engagement = (
        sum(m.engagement_score for m in week_metrics)
        / len(week_metrics) * 100
        if week_metrics else 0
    )

    # 3. Lecture Attendance Progress This Month
    month_ago = datetime.now() - timedelta(days=30)
    lectures_this_month = db.query(Lectures).filter(
        Lectures.date >= month_ago
    ).count()

    attended_this_month = db.query(AttendanceMetrics).filter(
        AttendanceMetrics.learner_id == learner_id,
        AttendanceMetrics.date >= month_ago
    ).count()

    monthly_goal_progress = {
        "current": attended_this_month,
        "target": 8,  # Example: 2 per week
        "percentage": (attended_this_month / 8) * 100
    }

    # 4. Topic Mastery (Top weakness)
    topic_mastery = db.query(
        TopicMastery.topic_id,
        func.max(TopicMastery.mastery_score).label('latest_score')
    ).filter(
        TopicMastery.learner_id == learner_id
    ).group_by(TopicMastery.topic_id).all()

    # 5. Recent Milestones
    recent_milestones = db.query(Milestones).filter(
        Milestones.learner_id == learner_id,
        Milestones.archived == False
    ).order_by(Milestones.earned_at.desc()).limit(5).all()

    # 6. Performance Trend (Last 4 weeks)
    four_weeks_ago = datetime.now() - timedelta(days=28)
    weekly_trends = calculate_weekly_trends(
        learner_id, four_weeks_ago, db
    )

    return {
        "learner_id": learner_id,
        "timestamp": datetime.now().isoformat(),

        # Tier 1: Primary Focus
        "streak": {
            "current": current_streak.current_streak,
            "longest": current_streak.longest_streak,
            "freeze_available": not current_streak.freeze_used,
            "days_until_risk": calculate_hours_until_midnight()
        },
        "weekly_engagement": weekly_engagement,
        "monthly_goal_progress": monthly_goal_progress,

        # Tier 2: Context
        "performance_trend": weekly_trends,

        # Tier 3: Details
        "topic_mastery_breakdown": topic_mastery,
        "recent_milestones": [
            {
                "type": m.milestone_type,
                "earned_at": m.earned_at.isoformat(),
                "display_name": get_milestone_display_name(m.milestone_type)
            }
            for m in recent_milestones
        ]
    }


@app.get("/api/dashboard/{learner_id}/deep-dive/{section}")
async def get_deep_dive(
    learner_id: int,
    section: str,  # 'lecture_quality', 'topic_mastery', 'engagement'
    db: Session = Depends(get_db)
):
    """
    Fetch detailed data for a specific dashboard section.
    """

    if section == "lecture_quality":
        # Last 10 lectures with quality metrics
        recent_lectures = db.query(Lectures).filter(
            Lectures.learner_id == learner_id
        ).order_by(Lectures.date.desc()).limit(10).all()

        return {
            "lectures": [
                {
                    "id": l.id,
                    "date": l.date,
                    "topic": l.topic,
                    "metrics": get_lecture_metrics(l.id, learner_id, db)
                }
                for l in recent_lectures
            ]
        }

    elif section == "topic_mastery":
        # All topics with current + historical mastery
        return get_topic_mastery_matrix(learner_id, db)

    elif section == "engagement":
        # 30-day engagement timeline
        return get_engagement_timeline(learner_id, db)
```

---

## Part 4: Frontend Component Examples

### React Component: Streak Display

```jsx
import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';

export const StreakComponent = ({ streakData }) => {
  const [timeUntilRisk, setTimeUntilRisk] = useState(null);

  useEffect(() => {
    // Update "time until midnight" countdown every minute
    const interval = setInterval(() => {
      const now = new Date();
      const midnight = new Date();
      midnight.setDate(midnight.getDate() + 1);
      midnight.setHours(0, 0, 0, 0);

      const hoursRemaining = Math.floor(
        (midnight - now) / (1000 * 60 * 60)
      );
      setTimeUntilRisk(hoursRemaining);
    }, 60000);

    return () => clearInterval(interval);
  }, []);

  const isStreakInDanger = timeUntilRisk && timeUntilRisk <= 2;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-gradient-to-br from-orange-50 to-red-50 p-6 rounded-lg border-2 border-orange-200"
    >
      <div className="text-center">
        <motion.div
          animate={{ scale: isStreakInDanger ? [1, 1.1, 1] : 1 }}
          transition={{
            duration: isStreakInDanger ? 1 : 0,
            repeat: isStreakInDanger ? Infinity : 0
          }}
          className="text-5xl mb-2"
        >
          🔥
        </motion.div>

        <div className="text-4xl font-bold text-orange-600 mb-1">
          {streakData.current} Days
        </div>

        <div className="text-gray-600 text-sm mb-4">
          Current Streak
        </div>

        {isStreakInDanger && (
          <div className="bg-red-100 border border-red-300 rounded px-3 py-2 mb-4">
            <p className="text-red-700 font-semibold text-sm">
              ⚠️ {timeUntilRisk} hours remaining!
            </p>
          </div>
        )}

        <div className="flex justify-between text-xs text-gray-600 mb-4">
          <span>Record: {streakData.longest} days</span>
          <span className={streakData.freeze_available ? "text-blue-600" : "text-gray-400"}>
            {streakData.freeze_available ? "❄️ Freeze Available" : "Freeze Used"}
          </span>
        </div>

        <button
          className="w-full bg-blue-500 hover:bg-blue-600 text-white py-2 rounded font-semibold transition"
          onClick={() => markTodayComplete()}
        >
          Mark as Complete ✓
        </button>
      </div>
    </motion.div>
  );
};
```

---

### React Component: Progress Bar with Dopamine Trigger

```jsx
import React, { useState, useEffect } from 'react';
import { motion } from 'framer-motion';

export const ProgressBar = ({
  current,
  target,
  label,
  showCelebration = true
}) => {
  const [showCelebrate, setShowCelebrate] = useState(false);
  const percentage = (current / target) * 100;

  // Trigger celebration when reaching milestones
  useEffect(() => {
    if (showCelebration && [25, 50, 75, 90, 100].includes(Math.round(percentage))) {
      setShowCelebrate(true);
      setTimeout(() => setShowCelebrate(false), 2000);
    }
  }, [percentage]);

  const colors = {
    0: 'from-gray-300 to-gray-400',
    25: 'from-blue-300 to-blue-400',
    50: 'from-blue-400 to-blue-500',
    75: 'from-emerald-400 to-emerald-500',
    90: 'from-emerald-500 to-emerald-600'
  };

  const getColor = () => {
    if (percentage >= 90) return colors[90];
    if (percentage >= 75) return colors[75];
    if (percentage >= 50) return colors[50];
    if (percentage >= 25) return colors[25];
    return colors[0];
  };

  return (
    <div className="w-full">
      <div className="flex justify-between mb-2">
        <label className="text-sm font-semibold text-gray-700">
          {label}
        </label>
        <span className="text-sm font-bold text-gray-900">
          {Math.round(percentage)}%
        </span>
      </div>

      <div className="w-full bg-gray-200 rounded-full h-3 overflow-hidden">
        <motion.div
          initial={{ width: 0 }}
          animate={{ width: `${percentage}%` }}
          transition={{
            duration: 0.6,
            ease: "easeOut"
          }}
          className={`h-full bg-gradient-to-r ${getColor()}`}
        />
      </div>

      <div className="flex justify-between text-xs text-gray-600 mt-1">
        <span>{current} of {target}</span>
        {percentage >= 90 && (
          <motion.span
            initial={{ opacity: 0, scale: 0.5 }}
            animate={{ opacity: 1, scale: 1 }}
            className="text-green-600 font-semibold"
          >
            Almost there! 🎯
          </motion.span>
        )}
      </div>

      {showCelebrate && (
        <CelebrationOverlay percentage={percentage} />
      )}
    </div>
  );
};

const CelebrationOverlay = ({ percentage }) => {
  const messages = {
    25: "Great start! 🎉",
    50: "Halfway there! 💪",
    75: "Almost done! 🔥",
    90: "You've got this! 🚀",
    100: "Perfect! 🏆"
  };

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0 }}
      className="absolute inset-0 flex items-center justify-center pointer-events-none"
    >
      <motion.div
        animate={{ y: [0, -20, 0] }}
        transition={{ duration: 1 }}
        className="text-center"
      >
        <p className="text-2xl font-bold text-green-600">
          {messages[Math.round(percentage)] || "Great work!"}
        </p>
      </motion.div>
    </motion.div>
  );
};
```

---

### React Component: Topic Mastery Matrix

```jsx
import React from 'react';
import { motion } from 'framer-motion';

export const TopicMasteryMatrix = ({ topics }) => {
  const getTrendIcon = (current, previous) => {
    if (!previous) return '→';
    if (current > previous) return '↗';
    if (current < previous) return '↘';
    return '→';
  };

  const getTrendColor = (current, previous) => {
    if (!previous) return 'gray';
    if (current > previous) return 'green';
    if (current < previous) return 'red';
    return 'gray';
  };

  const getMasteryColor = (score) => {
    if (score >= 80) return 'bg-green-100 text-green-800';
    if (score >= 60) return 'bg-yellow-100 text-yellow-800';
    return 'bg-red-100 text-red-800';
  };

  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b-2 border-gray-300">
            <th className="text-left font-bold text-gray-700 py-3 px-4">Topic</th>
            <th className="text-center font-bold text-gray-700 py-3 px-4">Current</th>
            <th className="text-center font-bold text-gray-700 py-3 px-4">Previous</th>
            <th className="text-center font-bold text-gray-700 py-3 px-4">Trend</th>
          </tr>
        </thead>
        <tbody>
          {topics.map((topic, idx) => (
            <motion.tr
              key={topic.id}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: idx * 0.05 }}
              className="border-b border-gray-200 hover:bg-gray-50"
            >
              <td className="py-3 px-4 font-medium text-gray-800">
                {topic.name}
              </td>

              <td className="py-3 px-4 text-center">
                <span className={`inline-block px-3 py-1 rounded-full font-semibold ${getMasteryColor(topic.current_score)}`}>
                  {Math.round(topic.current_score)}%
                </span>
              </td>

              <td className="py-3 px-4 text-center text-gray-600">
                {topic.previous_score ? `${Math.round(topic.previous_score)}%` : '—'}
              </td>

              <td className="py-3 px-4 text-center">
                <span className={`text-lg font-bold text-${getTrendColor(topic.current_score, topic.previous_score)}-600`}>
                  {getTrendIcon(topic.current_score, topic.previous_score)}
                  {topic.previous_score ? ` ${Math.round(topic.current_score - topic.previous_score):+d}%` : ''}
                </span>
              </td>
            </motion.tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};
```

---

### React Component: Chart - Performance Trend

```jsx
import React from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Area,
  AreaChart
} from 'recharts';

export const PerformanceTrend = ({ weeklyData }) => {
  return (
    <div className="w-full h-64 bg-white p-4 rounded-lg border border-gray-200">
      <h3 className="font-semibold text-gray-800 mb-4">4-Week Trend</h3>

      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={weeklyData}>
          <defs>
            <linearGradient id="colorEngagement" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.8}/>
              <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.1}/>
            </linearGradient>
          </defs>

          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />

          <XAxis
            dataKey="week"
            tick={{ fill: '#6b7280', fontSize: 12 }}
          />

          <YAxis
            domain={[0, 100]}
            tick={{ fill: '#6b7280', fontSize: 12 }}
          />

          <Tooltip
            contentStyle={{
              backgroundColor: '#fff',
              border: '1px solid #e5e7eb',
              borderRadius: '0.5rem'
            }}
            formatter={(value) => `${Math.round(value)}%`}
            labelFormatter={(label) => `Week ${label}`}
          />

          <Area
            type="monotone"
            dataKey="engagement"
            stroke="#3b82f6"
            strokeWidth={2}
            fillOpacity={1}
            fill="url(#colorEngagement)"
          />
        </AreaChart>
      </ResponsiveContainer>

      <div className="mt-4 grid grid-cols-2 gap-4 text-sm">
        <div>
          <p className="text-gray-600">Average</p>
          <p className="text-lg font-bold text-blue-600">
            {Math.round(weeklyData.reduce((a, w) => a + w.engagement, 0) / weeklyData.length)}%
          </p>
        </div>
        <div>
          <p className="text-gray-600">Trend</p>
          <p className="text-lg font-bold text-green-600">
            ↗ Improving
          </p>
        </div>
      </div>
    </div>
  );
};
```

---

### React Component: Milestone Achievements

```jsx
import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

const MILESTONES = {
  'first_lecture': {
    title: 'First Lecture',
    icon: '🎓',
    description: 'Completed your first lecture'
  },
  '3_day_streak': {
    title: '3-Day Streak',
    icon: '🔥',
    description: 'Attended 3 lectures in a row'
  },
  '7_day_streak': {
    title: '7-Day Streak',
    icon: '🔥🔥',
    description: 'Consistency champion!'
  },
  'perfect_score': {
    title: 'Perfect Score',
    icon: '💯',
    description: 'Scored 100% on an assignment'
  },
  'all_homework': {
    title: 'Homework Hero',
    icon: '📝',
    description: 'Completed all assignments in a week'
  }
};

export const MilestonesList = ({ milestones, enableAnimation = true }) => {
  const [expandedId, setExpandedId] = useState(null);

  return (
    <div className="space-y-3">
      <h3 className="font-semibold text-gray-800 mb-4">Achievements</h3>

      <AnimatePresence>
        {milestones.map((m, idx) => {
          const milestone = MILESTONES[m.type];

          return (
            <motion.div
              key={m.id}
              initial={enableAnimation ? { opacity: 0, x: -20 } : { opacity: 1 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: idx * 0.1 }}
              className="bg-gradient-to-r from-amber-50 to-orange-50 p-4 rounded-lg border border-amber-200 cursor-pointer hover:shadow-md transition"
              onClick={() => setExpandedId(expandedId === m.id ? null : m.id)}
            >
              <div className="flex items-start gap-3">
                <span className="text-3xl">{milestone.icon}</span>

                <div className="flex-1">
                  <div className="flex justify-between items-start">
                    <div>
                      <p className="font-bold text-amber-900">{milestone.title}</p>
                      <p className="text-sm text-amber-700">{milestone.description}</p>
                    </div>
                    <span className="text-xs text-amber-600">
                      {new Date(m.earned_at).toLocaleDateString()}
                    </span>
                  </div>

                  {expandedId === m.id && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: 'auto' }}
                      className="mt-3 pt-3 border-t border-amber-200 text-xs text-amber-800"
                    >
                      <p>🎉 Great achievement! You've unlocked this milestone through consistent effort.</p>
                    </motion.div>
                  )}
                </div>
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>

      {milestones.length === 0 && (
        <div className="text-center py-8 text-gray-500">
          <p className="text-sm">No achievements yet. Keep going! 🚀</p>
        </div>
      )}
    </div>
  );
};
```

---

## Part 5: Real-Time Updates with WebSocket

### WebSocket Handler for Live Updates

```python
# backend/websocket_manager.py
from fastapi import WebSocket
import asyncio
import json
from typing import Dict, List

class DashboardManager:
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, learner_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[learner_id] = websocket

    async def disconnect(self, learner_id: int):
        if learner_id in self.active_connections:
            del self.active_connections[learner_id]

    async def broadcast_metric_update(
        self,
        learner_id: int,
        metric_type: str,
        new_value: dict
    ):
        """Send metric update to connected client"""
        if learner_id in self.active_connections:
            try:
                await self.active_connections[learner_id].send_json({
                    "type": "metric_update",
                    "metric": metric_type,
                    "value": new_value,
                    "timestamp": datetime.now().isoformat()
                })
            except Exception as e:
                await self.disconnect(learner_id)

manager = DashboardManager()

@app.websocket("/ws/dashboard/{learner_id}")
async def websocket_endpoint(websocket: WebSocket, learner_id: int):
    await manager.connect(learner_id, websocket)
    try:
        while True:
            # Listen for client messages (e.g., requests for updates)
            data = await websocket.receive_json()

            if data.get("action") == "refresh_metrics":
                # Fetch fresh metrics and send
                fresh_data = await get_dashboard_data(learner_id, db)
                await websocket.send_json({
                    "type": "full_refresh",
                    "data": fresh_data
                })

    except Exception as e:
        await manager.disconnect(learner_id)
```

### Frontend WebSocket Connection

```jsx
// dashboard/useDashboardWebSocket.js
import { useEffect, useState, useCallback } from 'react';

export const useDashboardWebSocket = (learnerId) => {
  const [metrics, setMetrics] = useState(null);
  const [isConnected, setIsConnected] = useState(false);

  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(
      `${protocol}//${window.location.host}/ws/dashboard/${learnerId}`
    );

    ws.onopen = () => {
      setIsConnected(true);
      // Request initial data
      ws.send(JSON.stringify({ action: 'refresh_metrics' }));
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'metric_update') {
        // Update specific metric (e.g., engagement score just updated)
        setMetrics(prev => ({
          ...prev,
          [data.metric]: data.value
        }));

        // Trigger animation/celebration for certain updates
        if (data.metric === 'engagement_score') {
          triggerCelebration();
        }
      } else if (data.type === 'full_refresh') {
        setMetrics(data.data);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      setIsConnected(false);
    };

    ws.onclose = () => {
      setIsConnected(false);
    };

    return () => ws.close();
  }, [learnerId]);

  return { metrics, isConnected };
};
```

---

## Part 6: Mobile-First Responsive Grid

### Tailwind CSS Responsive Dashboard Layout

```jsx
export const DashboardLayout = ({ dashboardData }) => {
  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-50">
      {/* Header */}
      <div className="sticky top-0 bg-white shadow-sm z-10 p-4">
        <h1 className="text-2xl font-bold text-gray-900">
          Your Progress
        </h1>
      </div>

      {/* Main Grid - Responsive */}
      <div className="p-4 space-y-4 md:grid md:grid-cols-2 lg:grid-cols-3 md:gap-4 md:space-y-0">

        {/* TIER 1: Primary Focus (Takes full width on mobile) */}
        <div className="md:col-span-2 lg:col-span-1 space-y-4">
          <StreakComponent streakData={dashboardData.streak} />
          <ProgressBar
            current={dashboardData.weekly_engagement}
            target={100}
            label="Weekly Engagement"
          />
        </div>

        {/* TIER 2: Context */}
        <div className="space-y-4 md:col-span-1 lg:col-span-1">
          <PerformanceTrend weeklyData={dashboardData.performance_trend} />
        </div>

        {/* Monthly Goal */}
        <div className="md:col-span-1 lg:col-span-1 space-y-4">
          <GoalCard goalData={dashboardData.monthly_goal_progress} />
        </div>

        {/* TIER 3: Details (Full width at bottom) */}
        <div className="md:col-span-2 lg:col-span-3 space-y-4">
          <TopicMasteryMatrix topics={dashboardData.topic_mastery} />
        </div>

        <div className="md:col-span-2 lg:col-span-3 space-y-4">
          <MilestonesList milestones={dashboardData.milestones} />
        </div>

      </div>
    </div>
  );
};

// Mobile breakpoints:
// Default (mobile): Full width, vertical stack
// md (tablet): 2-column grid
// lg (desktop): 3-column grid
```

---

## Part 7: Notification Strategy

### Smart Notification Dispatcher

```python
# backend/notification_service.py
from enum import Enum
from datetime import datetime, timedelta

class NotificationType(Enum):
    LECTURE_REMINDER = "lecture_reminder"       # 30 min before
    STREAK_IN_DANGER = "streak_in_danger"       # Last 2 hours
    MILESTONE_UNLOCKED = "milestone_unlocked"   # Immediate
    IMPROVEMENT_ALERT = "improvement_alert"     # Daily digest
    WEEKLY_SUMMARY = "weekly_summary"            # Friday evening

class NotificationService:
    async def check_and_send_notifications(self, learner_id: int, db: Session):
        """
        Called periodically to check if notifications should be sent.
        Only sends actionable or celebratory notifications.
        """

        # 1. Lecture Reminder (30 min before start)
        upcoming_lecture = get_upcoming_lecture(learner_id, minutes=30, db=db)
        if upcoming_lecture:
            await send_notification(
                learner_id,
                NotificationType.LECTURE_REMINDER,
                f"Lecture starts in 30 minutes: {upcoming_lecture.topic}",
                db=db
            )

        # 2. Streak in Danger (Last 2 hours of the day)
        current_streak = get_current_streak(learner_id, db=db)
        if current_streak and current_streak.current > 0:
            hours_until_midnight = calculate_hours_until_midnight()
            if hours_until_midnight <= 2 and not attended_today(learner_id, db=db):
                await send_notification(
                    learner_id,
                    NotificationType.STREAK_IN_DANGER,
                    f"🔥 Your {current_streak.current}-day streak is in danger! "
                    f"{hours_until_midnight} hours remaining.",
                    db=db
                )

        # 3. Milestone Unlocked (Real-time, sent immediately when earned)
        # (Handled separately in activity_handler)

        # 4. Improvement Alert (Daily summary of improvements)
        daily_improvement = get_daily_improvement(learner_id, db=db)
        if daily_improvement and daily_improvement.total_improvement > 0:
            await send_notification(
                learner_id,
                NotificationType.IMPROVEMENT_ALERT,
                f"📈 You improved {daily_improvement.total_improvement:.0f}% today! "
                f"Keep it up!",
                db=db
            )

        # 5. Weekly Summary (Friday 18:00)
        if is_friday_evening(18):
            weekly_stats = get_weekly_stats(learner_id, db=db)
            await send_notification(
                learner_id,
                NotificationType.WEEKLY_SUMMARY,
                format_weekly_summary(weekly_stats),
                db=db
            )

async def send_notification(
    learner_id: int,
    notification_type: NotificationType,
    message: str,
    db: Session
):
    """
    Send notification via appropriate channel (WhatsApp, Email, Push)
    based on learner preferences.
    """
    learner = db.query(Learner).filter(Learner.id == learner_id).first()

    # Only send if user hasn't opted out
    if learner.notifications_enabled:
        notification = Notification(
            learner_id=learner_id,
            type=notification_type,
            message=message,
            created_at=datetime.now(),
            sent=False
        )
        db.add(notification)
        db.commit()

        # Send via WhatsApp (primary)
        if learner.whatsapp_enabled:
            await whatsapp_service.send_message(
                learner.whatsapp_number,
                message
            )
        # Fallback to Email
        elif learner.email:
            await email_service.send_email(
                learner.email,
                subject=notification_type.value.replace('_', ' ').title(),
                body=message
            )
        # Push notification
        else:
            await push_service.send(learner.device_token, message)

def format_weekly_summary(stats) -> str:
    """Format weekly summary for WhatsApp"""
    return f"""
📊 Weekly Summary

✅ Attended: {stats.lectures_attended}/5
📈 Engagement: {stats.avg_engagement:.0f}%
🔥 Streak: {stats.current_streak} days
📚 Topics Improved: {stats.topics_improved}

Top Achievement: {stats.top_milestone}

Great job this week! 🎉
Keep up the momentum next week!
"""
```

---

## Part 8: Testing the Dashboard

### Example Test Cases

```python
# tests/test_dashboard_metrics.py
import pytest
from datetime import datetime, timedelta
from app.dashboard import get_dashboard_data

@pytest.fixture
def learner_with_data(db):
    """Create test learner with sample metrics"""
    learner = Learner(id=1, name="Test Learner")
    db.add(learner)
    db.commit()

    # Add 4 weeks of engagement data
    for week in range(4):
        for day in range(7):
            metric = EngagementMetric(
                learner_id=1,
                date=datetime.now() - timedelta(days=28-week*7-day),
                engagement_score=0.6 + (week * 0.05) + (day % 2 * 0.05)
            )
            db.add(metric)
    db.commit()
    return learner

def test_streak_calculation(db, learner_with_data):
    """Test that current streak is calculated correctly"""
    dashboard = get_dashboard_data(1, db)
    assert dashboard['streak']['current'] >= 0

def test_weekly_engagement_average(db, learner_with_data):
    """Test weekly engagement percentage"""
    dashboard = get_dashboard_data(1, db)
    assert 0 <= dashboard['weekly_engagement'] <= 100

def test_trend_shows_improvement(db, learner_with_data):
    """Test that performance trend shows 4-week progression"""
    dashboard = get_dashboard_data(1, db)
    trends = dashboard['performance_trend']

    # Verify trend is upward
    assert len(trends) == 4
    assert trends[-1]['engagement'] > trends[0]['engagement']

def test_milestone_unlocked(db, learner_with_data):
    """Test that milestones are correctly awarded"""
    dashboard = get_dashboard_data(1, db)
    milestone_types = [m['type'] for m in dashboard['recent_milestones']]

    # Should have earned "first_lecture" at minimum
    assert 'first_lecture' in milestone_types
```

---

## Part 9: Performance Optimization

### Caching Strategy

```python
# backend/cache_service.py
from functools import lru_cache
import redis
import json

redis_client = redis.Redis(host='localhost', port=6379, db=0)

@lru_cache(maxsize=128)
def get_cached_dashboard(learner_id: int, cache_key: str):
    """
    Three-layer caching:
    1. In-memory cache (LRU) - fastest
    2. Redis cache - shared between processes
    3. Database - source of truth
    """
    cache_key = f"dashboard:{learner_id}:{cache_key}"

    # Try Redis first
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # Fallback to database query
    # Cache for 5 minutes
    redis_client.setex(cache_key, 300, json.dumps(result))
    return result

# Invalidate cache when metrics update
def invalidate_dashboard_cache(learner_id: int):
    """Clear cached dashboard when metrics change"""
    patterns = [
        f"dashboard:{learner_id}:*",
        f"metrics:{learner_id}:*",
        f"streak:{learner_id}:*"
    ]
    for pattern in patterns:
        for key in redis_client.scan_iter(match=pattern):
            redis_client.delete(key)
```

---

## Summary

This implementation guide covers:

1. **Data Model** - PostgreSQL schema for metrics storage
2. **Backend** - FastAPI endpoints with caching and optimization
3. **Frontend** - React components with animations and progressive disclosure
4. **Real-time Updates** - WebSocket for live metric updates
5. **Responsive Design** - Mobile-first Tailwind CSS layout
6. **Notifications** - Smart notification system avoiding message fatigue
7. **Testing** - Pytest examples for metric calculations
8. **Performance** - Multi-layer caching strategy

For your Training Agent context (Georgian language lectures), implement the modules in this order:

1. Database schema (metrics storage)
2. Core metrics calculation (streak, engagement)
3. API endpoints (dashboard data fetching)
4. Frontend components (React UI)
5. Real-time updates (WebSocket)
6. Notification system (WhatsApp/email alerts)
7. Testing and optimization
