"""OpenClaw / Chief Research Officer gateway — Paperclip HTTP adapter receiver.

Paperclip registers OpenClaw (id 1af8b41b-...) with adapter `http` pointing at
`POST http://localhost:8000/query`. This module provides the matching FastAPI
route. It mirrors the Training Agent Paperclip bridge contract (`/paperclip/task`):

- Auth: `Authorization: Bearer <PAPERCLIP_OPENCLAW_SECRET>`. Fail-closed.
- Payload: tolerant — accepts wrapped `{issue: {...}}` or flat `{issueId, title,
  description}`; unknown fields ignored.
- Response: 202 immediately with `{status, runId, issueId, intent}`; handlers run
  in a background task and post progress back to the Paperclip issue thread via
  the shared comment/status helpers in `tools.app.server`.

Runtime decision: lives on the same FastAPI host as the Training bridge. One
process, two routers, two independently-rotated secrets — documented in
`TECH_BASELINE.md §3`.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

openclaw_router = APIRouter(tags=["OpenClaw"])

SMOKE_KEYWORDS = (
    "smoke test",
    "smoke-test",
    "readiness",
    "gateway check",
    "bridge check",
)
RESEARCH_KEYWORDS = (
    "research",
    "investigate",
    "summarize",
    "summary",
    "brief",
    "analyze",
    "analyse",
    "compare",
    "landscape",
    "find out",
    "what is",
    "who is",
)


class OpenClawQueryPayload(BaseModel):
    """Incoming dispatch from Paperclip to OpenClaw.

    Paperclip's payload shape is not frozen — accept the minimum fields we need
    and ignore the rest so the gateway doesn't break when orchestrator evolves.
    """

    model_config = {"extra": "allow"}

    issue: dict[str, Any] | None = None
    issueId: str | None = None
    title: str | None = None
    description: str | None = None
    comments: list[dict[str, Any]] | None = None
    assignee: dict[str, Any] | None = None
    agentId: str | None = None
    runId: str | None = None
    context: dict[str, Any] | None = None
    query: str | None = None  # some callers may send a direct `query` field


def verify_openclaw_secret(authorization: str | None) -> None:
    """Validate the OpenClaw Bearer token. Fails closed on missing server secret."""
    # Lazy config import so tests can monkeypatch the module attribute.
    from tools.core import config

    secret = getattr(config, "PAPERCLIP_OPENCLAW_SECRET", "")
    if not secret:
        logger.error(
            "PAPERCLIP_OPENCLAW_SECRET not configured — rejecting /query request"
        )
        raise HTTPException(
            status_code=503,
            detail="Server misconfigured: PAPERCLIP_OPENCLAW_SECRET not set",
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    expected = f"Bearer {secret}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid OpenClaw secret")


def _extract_issue(payload: OpenClawQueryPayload) -> dict[str, Any]:
    """Flatten any of the three dispatch shapes we accept into one dict.

    Three shapes in the wild:
    1. Wrapped:        `{issue: {id, title, description, ...}}`
    2. Flat:           `{issueId, title, description}`
    3. Paperclip HTTP: `{agentId, runId, context: {issueId, ...}}` — the real
       production dispatch. `context` may also carry title/description when the
       orchestrator hydrates it; when it only carries `issueId` the BG handler
       is responsible for re-fetching the issue from Paperclip if it needs the
       body.
    """
    if payload.issue:
        return payload.issue
    ctx = payload.context or {}
    return {
        "id": payload.issueId or ctx.get("issueId") or ctx.get("taskId"),
        "title": payload.title or ctx.get("title"),
        "description": payload.description or ctx.get("description"),
    }


def classify_query_intent(title: str, description: str) -> str:
    """Pick an intent from task title + description.

    Returns one of: `smoke_test`, `research`, `unknown`. Simple substring match
    so the board can dispatch natural-language tasks without memorizing a
    strict schema.
    """
    text = f"{title}\n{description}".lower()
    if any(kw in text for kw in SMOKE_KEYWORDS):
        return "smoke_test"
    if any(kw in text for kw in RESEARCH_KEYWORDS):
        return "research"
    return "unknown"


async def _handle_smoke_test(issue_id: str, run_id: str | None, title: str) -> None:
    """Respond to a smoke-test dispatch with a readiness receipt + in_review."""
    from tools.app.server import post_paperclip_comment, set_paperclip_issue_status

    body = (
        "✅ OpenClaw (Chief Research Officer) gateway alive.\n\n"
        "Received smoke-test dispatch via `POST /query`, authenticated with "
        "`PAPERCLIP_OPENCLAW_SECRET`.\n\n"
        f"- issueId: `{issue_id}`\n"
        f"- runId: `{run_id or 'n/a'}`\n"
        f"- task: {title}\n\n"
        "Gateway contract confirmed:\n"
        "- Auth: fail-closed Bearer on `PAPERCLIP_OPENCLAW_SECRET`.\n"
        "- Payload: tolerant (wrapped `{issue: {...}}` or flat).\n"
        "- Intents today: `smoke_test`, `research`, `unknown`.\n"
        "- Response path: on-issue comment + status patch back via Paperclip API.\n\n"
        "Marking `in_review`. Ready to receive research queries."
    )
    await post_paperclip_comment(issue_id, body, run_id)
    await set_paperclip_issue_status(issue_id, "in_review")


async def _handle_research(
    issue_id: str, run_id: str | None, title: str, description: str
) -> None:
    """Acknowledge a research query and set status.

    The real research backend (Firecrawl + NotebookLM + Claude) is OpenClaw's
    own stack, wired in a follow-up. For the gateway smoke-test we acknowledge
    receipt so the dispatch is visible on the issue thread and the assignee
    heartbeat advances.
    """
    from tools.app.server import post_paperclip_comment, set_paperclip_issue_status

    preview = (description or title or "").strip()
    if len(preview) > 500:
        preview = preview[:500].rstrip() + "…"

    body = (
        "🔎 OpenClaw gateway received a research query.\n\n"
        f"- issueId: `{issue_id}`\n"
        f"- runId: `{run_id or 'n/a'}`\n"
        f"- title: {title}\n\n"
        "**Query (excerpt):**\n"
        f"> {preview or '(empty)'}\n\n"
        "Gateway ack only — the research reasoning backend (Firecrawl + "
        "NotebookLM + Claude) is wired in a follow-up subtask. When it lands, "
        "the on-issue comment will include a full GE-language brief with "
        "sources. For now, this confirms dispatch + auth + thread-post path."
    )
    await post_paperclip_comment(issue_id, body, run_id)
    await set_paperclip_issue_status(issue_id, "in_review")


async def _handle_unknown(issue_id: str, run_id: str | None, title: str) -> None:
    """Acknowledge unknown-intent tasks so the issue thread stays current."""
    from tools.app.server import post_paperclip_comment

    body = (
        "ℹ️ OpenClaw gateway received this task but could not classify a known "
        "intent. Echoing payload and leaving status unchanged.\n\n"
        f"- issueId: `{issue_id}`\n"
        f"- runId: `{run_id or 'n/a'}`\n"
        f"- title: {title}\n\n"
        "Known intents today: `smoke_test`, `research`. Tag the title with a "
        "matching keyword (for example `research`, `investigate`, `summarize`) "
        "to route the task into a specific workflow."
    )
    await post_paperclip_comment(issue_id, body, run_id)


def register_openclaw_routes(app, limiter) -> None:
    """Attach /query to `app`, rate-limited via the existing slowapi limiter.

    We register the handler here (instead of using `@openclaw_router.post`)
    because slowapi's `@limiter.limit` decorator needs the concrete limiter
    instance from `server.py`. server.py calls this during startup wiring.
    """

    @app.post("/query")
    @limiter.limit("30/minute")
    async def openclaw_query(  # noqa: D401 — FastAPI handler
        request: Request,
        background_tasks: BackgroundTasks,
        authorization: str | None = Header(None),
    ):
        """Receive a task dispatched by Paperclip to OpenClaw / CRO.

        Auth: `Authorization: Bearer <PAPERCLIP_OPENCLAW_SECRET>` (401 on
        mismatch, 503 if server secret unset).

        Returns 202 + JSON `{status, runId, issueId, intent}`. The matched
        workflow runs in a background task and posts progress back to the
        Paperclip issue thread.

        Parses the body manually rather than via a Pydantic dependency so
        bad/missing fields produce clear 422 diagnostics and the raw payload
        is logged for incident debugging when Paperclip's adapter shape
        evolves.
        """
        verify_openclaw_secret(authorization)

        raw_body = await request.body()
        try:
            data = await request.json() if raw_body else {}
        except Exception as exc:
            logger.warning(
                "[openclaw] invalid JSON body len=%d preview=%r: %s",
                len(raw_body), raw_body[:200], exc,
            )
            raise HTTPException(
                status_code=422, detail="Body must be valid JSON"
            )
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=422, detail="Body must be a JSON object"
            )
        try:
            payload = OpenClawQueryPayload(**data)
        except Exception as exc:
            logger.warning(
                "[openclaw] payload validation failed body=%r err=%s",
                raw_body[:500], exc,
            )
            raise HTTPException(
                status_code=422, detail=f"Payload validation: {exc}"
            )
        logger.info(
            "[openclaw] dispatch received body_len=%d keys=%s",
            len(raw_body), sorted(data.keys()),
        )

        issue = _extract_issue(payload)
        issue_id = issue.get("id") or payload.issueId
        if not issue_id:
            raise HTTPException(status_code=422, detail="Missing issue id")

        title = (issue.get("title") or payload.title or "").strip()
        description = (
            issue.get("description") or payload.description or payload.query or ""
        ).strip()
        run_id = payload.runId or str(uuid.uuid4())

        # Paperclip's HTTP adapter ships `context.issueId` only — no title or
        # description. Fetch from the Paperclip API so intent classification
        # has real data to work with; gracefully fall back to "unknown" on
        # fetch failure.
        if not title and not description:
            from tools.app.server import fetch_paperclip_issue
            fetched = await fetch_paperclip_issue(issue_id)
            if fetched:
                title = (fetched.get("title") or "").strip()
                description = (fetched.get("description") or "").strip()
                logger.info(
                    "[openclaw] fetched issue body issue=%s title=%r",
                    issue_id, title[:80],
                )

        intent = classify_query_intent(title, description)
        logger.info(
            "[openclaw] Received query issue=%s run=%s intent=%s title=%r",
            issue_id, run_id, intent, title[:80],
        )

        if intent == "smoke_test":
            background_tasks.add_task(_handle_smoke_test, issue_id, run_id, title)
        elif intent == "research":
            background_tasks.add_task(
                _handle_research, issue_id, run_id, title, description
            )
        else:
            background_tasks.add_task(_handle_unknown, issue_id, run_id, title)

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "runId": run_id,
                "issueId": issue_id,
                "intent": intent,
            },
        )

    logger.info("[openclaw] /query route registered on FastAPI app")
