"""Tests for the Paperclip /paperclip/task bridge in tools/app/server.py.

Covers:
- classify_paperclip_intent — intent routing for smoke_test / process_recording /
  pre_meeting_reminder / unknown
- _extract_issue_fields — flat payload, wrapped {"issue": {...}} payload, id fallbacks
- verify_paperclip_secret — fail-closed when secret is unset (503), missing header
  (401), mismatched secret (401), correct secret (no raise)
- Endpoint POST /paperclip/task — 401 without auth, 503 when secret unset, 202 on
  valid dispatch with echoed runId/issueId/intent, 422 when no issue id supplied
- _dispatch_paperclip_task — routes smoke_test → _handle_smoke_test, unknown →
  _handle_unknown, other → _handle_acknowledged

Follows the test_server_new.py bootstrap pattern: pops fastapi/slowapi/httpx/
pydantic/tools.app.server stubs so real FastAPI + httpx TestClient work.

Run with:
    pytest tools/tests/test_paperclip_bridge.py -v
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Same bootstrap as test_server_new.py — real FastAPI for TestClient behavior.
# ---------------------------------------------------------------------------
_popped_stubs: dict[str, object] = {}
for _mod_name in list(sys.modules):
    if _mod_name.startswith(
        ("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server")
    ):
        _popped_stubs[_mod_name] = sys.modules.pop(_mod_name)


from httpx import ASGITransport, AsyncClient  # noqa: E402

import tools.app.server as srv  # noqa: E402
from tools.app.server import (  # noqa: E402
    _extract_issue_fields,
    _dispatch_paperclip_task,
    app,
    classify_paperclip_intent,
    verify_paperclip_secret,
)

_TEST_SECRET = "paperclip-test-secret-xyz"
_AUTH_OK = {"Authorization": f"Bearer {_TEST_SECRET}"}


@pytest.fixture(autouse=True)
def _reset_limiter():
    srv.limiter.reset()
    yield
    srv.limiter.reset()


@pytest.fixture
def secret_configured():
    with patch.object(srv, "PAPERCLIP_WEBHOOK_SECRET", _TEST_SECRET):
        yield


@pytest.fixture
def secret_unset():
    with patch.object(srv, "PAPERCLIP_WEBHOOK_SECRET", ""):
        yield


# ===========================================================================
# classify_paperclip_intent
# ===========================================================================


class TestClassifyPaperclipIntent:
    def test_smoke_test_title(self):
        assert classify_paperclip_intent("Smoke test the bridge", "") == "smoke_test"

    def test_smoke_test_case_insensitive(self):
        assert classify_paperclip_intent("SMOKE TEST", "") == "smoke_test"

    def test_smoke_test_in_description(self):
        assert (
            classify_paperclip_intent("Ping", "Please run a smoke test now")
            == "smoke_test"
        )

    def test_process_recording(self):
        assert (
            classify_paperclip_intent("Process recording for group 1", "")
            == "process_recording"
        )

    def test_pre_meeting_reminder_hyphen(self):
        assert (
            classify_paperclip_intent("Send pre-meeting reminder", "")
            == "pre_meeting_reminder"
        )

    def test_pre_meeting_reminder_space(self):
        assert (
            classify_paperclip_intent("Pre meeting nudge", "")
            == "pre_meeting_reminder"
        )

    def test_reminder_keyword_alone(self):
        assert (
            classify_paperclip_intent("Lecture reminder at 17:45", "")
            == "pre_meeting_reminder"
        )

    def test_unknown_falls_through(self):
        assert (
            classify_paperclip_intent("Refactor admin dashboard", "unrelated body")
            == "unknown"
        )

    def test_empty_strings(self):
        assert classify_paperclip_intent("", "") == "unknown"


# ===========================================================================
# _extract_issue_fields
# ===========================================================================


class TestExtractIssueFields:
    def test_flat_payload(self):
        payload = {
            "id": "iss-1",
            "identifier": "AIP-9",
            "title": "Build bridge",
            "description": "POST endpoint",
            "status": "in_progress",
            "runId": "run-abc",
        }
        fields = _extract_issue_fields(payload)
        assert fields["issueId"] == "iss-1"
        assert fields["identifier"] == "AIP-9"
        assert fields["title"] == "Build bridge"
        assert fields["description"] == "POST endpoint"
        assert fields["status"] == "in_progress"
        assert fields["runId"] == "run-abc"

    def test_wrapped_payload(self):
        payload = {
            "issue": {
                "id": "iss-2",
                "identifier": "AIP-10",
                "title": "Smoke test",
                "description": "",
                "status": "todo",
            },
            "runId": "run-xyz",
        }
        fields = _extract_issue_fields(payload)
        assert fields["issueId"] == "iss-2"
        assert fields["identifier"] == "AIP-10"
        assert fields["title"] == "Smoke test"
        assert fields["runId"] == "run-xyz"

    def test_run_id_snake_case_fallback(self):
        fields = _extract_issue_fields(
            {"id": "iss-3", "title": "x", "run_id": "snake-run"}
        )
        assert fields["runId"] == "snake-run"

    def test_run_id_execution_run_id_fallback(self):
        fields = _extract_issue_fields(
            {"issue": {"id": "iss-4", "executionRunId": "exec-run-1"}}
        )
        assert fields["runId"] == "exec-run-1"

    def test_missing_issue_id_returns_none(self):
        fields = _extract_issue_fields({"title": "orphan"})
        assert fields["issueId"] is None

    def test_empty_defaults(self):
        fields = _extract_issue_fields({"id": "iss-5"})
        assert fields["title"] == ""
        assert fields["description"] == ""
        assert fields["runId"] == ""

    def test_paperclip_http_adapter_native_payload(self):
        """Paperclip's HTTP adapter sends {agentId, runId, context:{issueId,taskKey,...}}.

        Source: @paperclipai/server/dist/adapters/http/execute.js. No title/
        description in the body — extractor must surface issueId via context.
        """
        payload = {
            "agentId": "agent-uuid",
            "runId": "run-42",
            "context": {
                "issueId": "iss-42",
                "taskId": "task-42",
                "taskKey": "AIP-42",
                "projectId": None,
                "projectWorkspaceId": None,
            },
        }
        fields = _extract_issue_fields(payload)
        assert fields["issueId"] == "iss-42"
        assert fields["identifier"] == "AIP-42"
        assert fields["runId"] == "run-42"
        assert fields["title"] == ""
        assert fields["description"] == ""

    def test_context_with_payload_template_fields(self):
        """payloadTemplate fields sit at the top level alongside context."""
        payload = {
            "source": "paperclip",
            "agentId": "agent-uuid",
            "runId": "run-7",
            "context": {"issueId": "iss-7", "taskKey": "AIP-7"},
        }
        fields = _extract_issue_fields(payload)
        assert fields["issueId"] == "iss-7"
        assert fields["identifier"] == "AIP-7"

    def test_flat_payload_takes_precedence_over_context(self):
        """Explicit flat fields win over context (backward compat)."""
        payload = {
            "id": "iss-explicit",
            "identifier": "AIP-EXPLICIT",
            "context": {"issueId": "iss-context", "taskKey": "AIP-CTX"},
        }
        fields = _extract_issue_fields(payload)
        assert fields["issueId"] == "iss-explicit"
        assert fields["identifier"] == "AIP-EXPLICIT"


# ===========================================================================
# verify_paperclip_secret
# ===========================================================================


class TestVerifyPaperclipSecret:
    def test_unset_secret_raises_503(self, secret_unset):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            verify_paperclip_secret("Bearer anything")
        assert exc.value.status_code == 503

    def test_missing_header_raises_401(self, secret_configured):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            verify_paperclip_secret(None)
        assert exc.value.status_code == 401

    def test_wrong_secret_raises_401(self, secret_configured):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            verify_paperclip_secret("Bearer wrong-secret")
        assert exc.value.status_code == 401

    def test_correct_secret_does_not_raise(self, secret_configured):
        verify_paperclip_secret(f"Bearer {_TEST_SECRET}")


# ===========================================================================
# _dispatch_paperclip_task routing
# ===========================================================================


@pytest.mark.asyncio
class TestDispatchPaperclipTask:
    async def test_smoke_test_routes_to_smoke_handler(self):
        fields = {"issueId": "iss-1", "runId": "r1", "title": "", "description": ""}
        with (
            patch.object(srv, "_handle_smoke_test", new_callable=AsyncMock) as smoke,
            patch.object(srv, "_handle_unknown", new_callable=AsyncMock) as unknown,
            patch.object(
                srv, "_handle_acknowledged", new_callable=AsyncMock
            ) as ack,
        ):
            await _dispatch_paperclip_task(fields, "smoke_test")
        smoke.assert_awaited_once_with(fields)
        unknown.assert_not_awaited()
        ack.assert_not_awaited()

    async def test_unknown_routes_to_unknown_handler(self):
        fields = {"issueId": "iss-2", "runId": "", "title": "", "description": ""}
        with (
            patch.object(srv, "_handle_smoke_test", new_callable=AsyncMock) as smoke,
            patch.object(srv, "_handle_unknown", new_callable=AsyncMock) as unknown,
            patch.object(
                srv, "_handle_acknowledged", new_callable=AsyncMock
            ) as ack,
        ):
            await _dispatch_paperclip_task(fields, "unknown")
        unknown.assert_awaited_once_with(fields)
        smoke.assert_not_awaited()
        ack.assert_not_awaited()

    async def test_process_recording_routes_to_acknowledged(self):
        fields = {"issueId": "iss-3", "runId": "", "title": "", "description": ""}
        with (
            patch.object(srv, "_handle_smoke_test", new_callable=AsyncMock) as smoke,
            patch.object(srv, "_handle_unknown", new_callable=AsyncMock) as unknown,
            patch.object(
                srv, "_handle_acknowledged", new_callable=AsyncMock
            ) as ack,
        ):
            await _dispatch_paperclip_task(fields, "process_recording")
        ack.assert_awaited_once_with(fields, "process_recording")
        smoke.assert_not_awaited()
        unknown.assert_not_awaited()

    async def test_handler_exception_is_swallowed(self):
        fields = {"issueId": "iss-4", "runId": "", "title": "", "description": ""}
        with patch.object(
            srv, "_handle_unknown", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            # Must not raise.
            await _dispatch_paperclip_task(fields, "unknown")


# ===========================================================================
# POST /paperclip/task — integration via real TestClient
# ===========================================================================


@pytest.mark.asyncio
class TestPaperclipEndpoint:
    async def test_missing_auth_returns_401(self, secret_configured):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as ac:
            resp = await ac.post(
                "/paperclip/task",
                json={"id": "iss-1", "title": "smoke test"},
            )
        assert resp.status_code == 401

    async def test_wrong_secret_returns_401(self, secret_configured):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as ac:
            resp = await ac.post(
                "/paperclip/task",
                json={"id": "iss-1", "title": "smoke test"},
                headers={"Authorization": "Bearer wrong"},
            )
        assert resp.status_code == 401

    async def test_unset_secret_returns_503(self, secret_unset):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as ac:
            resp = await ac.post(
                "/paperclip/task",
                json={"id": "iss-1", "title": "smoke test"},
                headers=_AUTH_OK,
            )
        assert resp.status_code == 503

    async def test_valid_flat_payload_returns_202(self, secret_configured):
        with patch.object(
            srv, "_dispatch_paperclip_task", new_callable=AsyncMock
        ) as dispatch:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as ac:
                resp = await ac.post(
                    "/paperclip/task",
                    json={
                        "id": "iss-1",
                        "identifier": "AIP-9",
                        "title": "Run smoke test on bridge",
                        "description": "",
                        "runId": "run-123",
                    },
                    headers=_AUTH_OK,
                )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["issueId"] == "iss-1"
        assert body["runId"] == "run-123"
        assert body["intent"] == "smoke_test"
        dispatch.assert_called_once()

    async def test_valid_wrapped_payload_returns_202(self, secret_configured):
        with patch.object(
            srv, "_dispatch_paperclip_task", new_callable=AsyncMock
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as ac:
                resp = await ac.post(
                    "/paperclip/task",
                    json={
                        "issue": {
                            "id": "iss-2",
                            "identifier": "AIP-10",
                            "title": "Process recording group 1",
                            "description": "",
                        },
                        "runId": "run-456",
                    },
                    headers=_AUTH_OK,
                )
        assert resp.status_code == 202
        body = resp.json()
        assert body["issueId"] == "iss-2"
        assert body["intent"] == "process_recording"

    async def test_missing_issue_id_returns_202_idle_wake(
        self, secret_configured
    ):
        """Bare payload without issueId — treated as idle-wake no-op.

        Regression for AIP-42 follow-up: Paperclip's manual/on-demand wake
        context is {actorId, wakeSource, triggeredBy, ...} with no issueId.
        Returning 422 flipped the adapter run to failed and kept the agent
        stuck in `error`. Handler must return 202 so the run succeeds.
        """
        with patch.object(
            srv, "_dispatch_paperclip_task", new_callable=AsyncMock
        ) as dispatch:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as ac:
                resp = await ac.post(
                    "/paperclip/task",
                    json={"title": "no id here"},
                    headers=_AUTH_OK,
                )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["issueId"] is None
        assert body["intent"] == "idle_wake"
        dispatch.assert_not_called()

    async def test_paperclip_manual_wake_context_returns_202(
        self, secret_configured
    ):
        """Native Paperclip on_demand wake payload: no issueId in context.

        This is the exact shape Paperclip posts when the board fires
        POST /api/agents/{id}/wakeup with no linked issue. Before the fix,
        the 422 response marked the heartbeat-run as failed and pinned the
        agent to status `error`.
        """
        with patch.object(
            srv, "_dispatch_paperclip_task", new_callable=AsyncMock
        ) as dispatch, patch.object(
            srv, "fetch_paperclip_issue", new_callable=AsyncMock
        ) as fetch:
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as ac:
                resp = await ac.post(
                    "/paperclip/task",
                    json={
                        "agentId": "agent-uuid",
                        "runId": "run-wake-1",
                        "context": {
                            "actorId": "local-board",
                            "wakeSource": "on_demand",
                            "triggeredBy": "board",
                            "forceFreshSession": False,
                            "wakeTriggerDetail": "manual",
                        },
                    },
                    headers=_AUTH_OK,
                )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["issueId"] is None
        assert body["runId"] == "run-wake-1"
        assert body["intent"] == "idle_wake"
        fetch.assert_not_awaited()
        dispatch.assert_not_called()

    async def test_paperclip_native_payload_hydrates_and_returns_202(
        self, secret_configured
    ):
        """Native Paperclip HTTP-adapter body: {agentId, runId, context}.

        No title/description in the body — the handler must call
        fetch_paperclip_issue() to hydrate, then classify intent correctly.
        Regression test for AIP-42 (422 on heartbeat POST).
        """
        hydrated_issue = {
            "id": "iss-42",
            "identifier": "AIP-42",
            "title": "Run smoke test on the bridge",
            "description": "bridge smoke test",
            "status": "in_progress",
        }
        with patch.object(
            srv, "_dispatch_paperclip_task", new_callable=AsyncMock
        ) as dispatch, patch.object(
            srv, "fetch_paperclip_issue", new_callable=AsyncMock
        ) as fetch:
            fetch.return_value = hydrated_issue
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as ac:
                resp = await ac.post(
                    "/paperclip/task",
                    json={
                        "agentId": "agent-uuid",
                        "runId": "run-42",
                        "context": {
                            "issueId": "iss-42",
                            "taskId": "task-42",
                            "taskKey": "AIP-42",
                            "projectId": None,
                            "projectWorkspaceId": None,
                        },
                    },
                    headers=_AUTH_OK,
                )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["issueId"] == "iss-42"
        assert body["runId"] == "run-42"
        assert body["intent"] == "smoke_test"
        fetch.assert_awaited_once_with("iss-42")
        dispatch.assert_called_once()
