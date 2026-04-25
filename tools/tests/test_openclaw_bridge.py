"""Tests for the OpenClaw / CRO Paperclip gateway (tools/app/openclaw_bridge.py).

Covers:
- `classify_query_intent` — natural-language substring routing
- `_extract_issue` — wrapped vs flat payload normalization
- `POST /query` auth rejection (missing, wrong secret, 503 fail-closed)
- `POST /query` accepts valid Bearer and returns 202 + intent routing
- Background tasks are scheduled for each intent path

Run with:
    pytest tools/tests/test_openclaw_bridge.py -v
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

# Pop stubs so we load real fastapi/slowapi/httpx/pydantic for ASGI testing.
# IDEMPOTENT: skip the pop if a sibling test file already swapped in the real
# modules — re-popping would create a second set of class objects and break
# class-identity checks across files (e.g. ``isinstance(e, HTTPException)``).
_fastapi_real = getattr(sys.modules.get("fastapi"), "__file__", None) is not None
if not _fastapi_real:
    for _mod in list(sys.modules):
        if _mod.startswith(
            ("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server",
             "tools.app.openclaw_bridge")
        ):
            sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import tools.app.server as srv  # noqa: E402
from tools.app.openclaw_bridge import (  # noqa: E402
    OpenClawQueryPayload,
    _extract_issue,
    classify_query_intent,
)
from tools.core import config as cfg  # noqa: E402

app = srv.app

_TEST_SECRET = "openclaw-test-secret"
_AUTH = {"Authorization": f"Bearer {_TEST_SECRET}"}


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    srv.limiter.reset()
    yield
    srv.limiter.reset()


@pytest.fixture
def patched_secret():
    with patch.object(cfg, "PAPERCLIP_OPENCLAW_SECRET", _TEST_SECRET):
        yield


@pytest.fixture
def stub_outbound():
    """Stub comment/status helpers so tests don't hit real Paperclip.

    Patches by import path (not via :data:`srv`) so we always hit the
    module currently registered in :data:`sys.modules`. Other test files
    (``test_paperclip_bridge.py``) re-import ``tools.app.server`` and
    swap the entry, leaving our module-level :data:`srv` orphaned.
    """
    with (
        patch("tools.app.server.post_paperclip_comment", AsyncMock()) as mock_post,
        patch("tools.app.server.set_paperclip_issue_status", AsyncMock()) as mock_patch,
    ):
        yield mock_post, mock_patch


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


@pytest.fixture(scope="module")
def sync_client():
    """Sync TestClient without lifespan context.

    base_url pinned to http://localhost so TrustedHostMiddleware's allowlist
    (localhost, 127.0.0.1) accepts the request. Not entered as a context
    manager — that would trigger the full app startup (Zoom, Pinecone, Mem0)
    which is slow and irrelevant to gateway unit tests. Starlette's TestClient
    still runs BackgroundTasks synchronously before returning the response.
    """
    return TestClient(app, base_url="http://localhost")


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


class TestClassifyIntent:
    def test_smoke_test_keyword(self):
        assert classify_query_intent("Smoke test the gateway", "") == "smoke_test"

    def test_gateway_check_keyword(self):
        assert classify_query_intent("Gateway check", "") == "smoke_test"

    def test_readiness_in_description(self):
        assert classify_query_intent("anything", "readiness probe") == "smoke_test"

    def test_research_keyword(self):
        assert (
            classify_query_intent("Research the Georgian AI news landscape", "")
            == "research"
        )

    def test_investigate_keyword(self):
        assert classify_query_intent("Investigate Claude 4.7 release", "") == "research"

    def test_summarize_keyword(self):
        assert (
            classify_query_intent("Summarize this paper", "text…") == "research"
        )

    def test_unknown_fallback(self):
        assert (
            classify_query_intent("Refactor the orchestrator", "") == "unknown"
        )

    def test_case_insensitive(self):
        assert classify_query_intent("SMOKE TEST", "") == "smoke_test"

    def test_smoke_beats_research(self):
        # Smoke test is evaluated first and wins when both keywords appear.
        assert (
            classify_query_intent("Smoke test the research pipeline", "")
            == "smoke_test"
        )


class TestExtractIssue:
    def test_wrapped_payload(self):
        payload = OpenClawQueryPayload(
            issue={"id": "i-1", "title": "wrapped", "description": "desc"}
        )
        assert _extract_issue(payload) == {
            "id": "i-1",
            "title": "wrapped",
            "description": "desc",
        }

    def test_flat_payload(self):
        payload = OpenClawQueryPayload(
            issueId="i-2", title="flat", description="fd"
        )
        assert _extract_issue(payload) == {
            "id": "i-2",
            "title": "flat",
            "description": "fd",
        }

    def test_wrapped_wins_over_flat(self):
        payload = OpenClawQueryPayload(
            issue={"id": "w-1", "title": "from-wrap"},
            issueId="i-x",
            title="from-flat",
        )
        assert _extract_issue(payload)["id"] == "w-1"
        assert _extract_issue(payload)["title"] == "from-wrap"

    def test_paperclip_http_adapter_context_shape(self):
        """Paperclip's HTTP adapter dispatches {agentId, runId, context:{issueId}}.

        Context carries only issueId/taskId/taskKey — no title/description. The
        extractor must still produce a usable `id` so the handler can proceed
        and fetch the body from the Paperclip API before intent classification.
        """
        payload = OpenClawQueryPayload(
            agentId="1af8b41b-500c-42ee-ac52-1678f57c1e44",
            runId="run-abc",
            context={
                "issueId": "8bb33462-86bb-4545-a657-b66863df8104",
                "taskId": "task-xyz",
                "taskKey": "AIP-42",
            },
        )
        extracted = _extract_issue(payload)
        assert extracted["id"] == "8bb33462-86bb-4545-a657-b66863df8104"
        assert extracted["title"] is None
        assert extracted["description"] is None

    def test_context_taskid_fallback(self):
        """If context has only taskId (not issueId), still produce an id."""
        payload = OpenClawQueryPayload(
            context={"taskId": "t-only"},
        )
        assert _extract_issue(payload)["id"] == "t-only"


# ---------------------------------------------------------------------------
# Endpoint integration tests (ASGI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOpenClawAuth:
    async def test_missing_authorization_returns_401(self, patched_secret):
        async with await _client() as c:
            r = await c.post(
                "/query", json={"issueId": "i-1", "title": "x"}
            )
        assert r.status_code == 401

    async def test_wrong_secret_returns_401(self, patched_secret):
        async with await _client() as c:
            r = await c.post(
                "/query",
                headers={"Authorization": "Bearer not-the-right-one"},
                json={"issueId": "i-1", "title": "x"},
            )
        assert r.status_code == 401

    async def test_missing_server_secret_returns_503(self):
        with patch.object(cfg, "PAPERCLIP_OPENCLAW_SECRET", ""):
            async with await _client() as c:
                r = await c.post(
                    "/query",
                    headers=_AUTH,
                    json={"issueId": "i-1", "title": "x"},
                )
        assert r.status_code == 503


class TestOpenClawDispatch:
    """Sync-client dispatch tests.

    Uses `fastapi.testclient.TestClient` (not httpx AsyncClient) because
    TestClient runs BackgroundTasks synchronously before returning the
    response — the async ASGITransport path can exit the mock-patch context
    before the BG task fires, causing false negatives on assert_awaited_once.
    """

    def test_smoke_test_returns_202_and_schedules_handler(
        self, sync_client, patched_secret, stub_outbound
    ):
        r = sync_client.post(
            "/query",
            headers=_AUTH,
            json={
                "issueId": "issue-smoke",
                "title": "Smoke test the OpenClaw gateway",
                "description": "",
                "runId": "run-xyz",
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "accepted"
        assert body["issueId"] == "issue-smoke"
        assert body["runId"] == "run-xyz"
        assert body["intent"] == "smoke_test"

        mock_post, mock_patch = stub_outbound
        mock_post.assert_awaited_once()
        mock_patch.assert_awaited_once_with("issue-smoke", "in_review")

    def test_research_intent_posts_comment_and_flips_status(
        self, sync_client, patched_secret, stub_outbound
    ):
        r = sync_client.post(
            "/query",
            headers=_AUTH,
            json={
                "issueId": "issue-research",
                "title": "Research the Georgian AI startup scene",
                "description": "Which companies deploy LLMs in GE today?",
                "runId": "rn-2",
            },
        )
        assert r.status_code == 202
        assert r.json()["intent"] == "research"

        mock_post, mock_patch = stub_outbound
        mock_post.assert_awaited_once()
        mock_patch.assert_awaited_once_with("issue-research", "in_review")

    def test_unknown_intent_acknowledges_without_status_change(
        self, sync_client, patched_secret, stub_outbound
    ):
        r = sync_client.post(
            "/query",
            headers=_AUTH,
            json={
                "issueId": "issue-unk",
                "title": "Refactor the orchestrator",
                "description": "",
            },
        )
        assert r.status_code == 202
        assert r.json()["intent"] == "unknown"
        mock_post, mock_patch = stub_outbound
        mock_post.assert_awaited_once()
        mock_patch.assert_not_awaited()

    def test_wrapped_issue_payload(self, sync_client, patched_secret, stub_outbound):
        r = sync_client.post(
            "/query",
            headers=_AUTH,
            json={
                "issue": {
                    "id": "wrapped-1",
                    "title": "Smoke test gateway",
                    "description": "",
                },
                "runId": "r-1",
            },
        )
        assert r.status_code == 202
        assert r.json()["issueId"] == "wrapped-1"

    def test_missing_issue_id_returns_422(
        self, sync_client, patched_secret, stub_outbound
    ):
        r = sync_client.post(
            "/query",
            headers=_AUTH,
            json={"title": "Smoke test"},
        )
        assert r.status_code == 422

    def test_generates_run_id_when_missing(
        self, sync_client, patched_secret, stub_outbound
    ):
        r = sync_client.post(
            "/query",
            headers=_AUTH,
            json={
                "issueId": "issue-norun",
                "title": "Smoke test",
            },
        )
        assert r.status_code == 202
        run_id = r.json()["runId"]
        assert run_id
        assert len(run_id) >= 8
