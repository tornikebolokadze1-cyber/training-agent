# 002: Hybrid n8n + Python Architecture

## Date
2026-03-14

## Status
accepted

## Context
The Training Agent needs to automate a complex workflow: pre-meeting reminders, Zoom recording downloads, AI-powered lecture analysis (2+ hour videos), Google Drive uploads, and WhatsApp notifications. The question was whether to build everything in n8n, everything in Python, or use a hybrid approach.

## Decision
Use a **hybrid architecture**: n8n for orchestration and lightweight triggers, Python for heavy computation.

- **n8n handles**: scheduling, triggers, email/WhatsApp notification routing, Zoom webhook reception
- **Python handles**: large file downloads (Zoom recordings), Gemini multimodal analysis (1M+ tokens), Google Drive resumable uploads, WhatsApp assistant (მრჩეველი) with RAG

## Reasoning
1. **n8n limitations**: n8n Code nodes have execution time limits and memory constraints that make processing 2-hour video files impossible. Gemini API calls with 1M+ tokens would timeout.
2. **n8n strengths**: Visual workflow builder is excellent for scheduling, triggers, and simple HTTP notifications. Easy to modify timing without code changes.
3. **Python strengths**: No execution limits, full async support, direct SDK access to Gemini/Claude/Pinecone, resumable upload capability for large files.
4. **Communication**: n8n sends webhook to Python server, Python processes and calls back to n8n (or sends notifications directly via Green API).

## Consequences
- **Positive**: Each tool used for its strengths. Pipeline can handle any video length. Easy to add new analysis stages in Python.
- **Negative**: Two systems to maintain. Debugging requires checking both n8n execution logs and Python logs. Deployment is more complex (Railway for Python + n8n cloud).
- **Trade-off accepted**: Operational complexity is worth the capability gain.
