"""Tests for the unified API resilience layer."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tools.core.api_resilience import (
    CircuitOpenError,
    CircuitState,
    _CircuitMetrics,
    _classify_error,
    _extract_status_code,
    _get_circuit,
    _is_auth_error,
    _is_claude_overloaded,
    _is_quota_error,
    _is_server_error,
    _is_timeout,
    get_circuit_status,
    resilient_api_call,
)


# ---------------------------------------------------------------------------
# Error classification tests
# ---------------------------------------------------------------------------


class TestErrorClassification:
    """Tests for error type detection functions."""

    def test_extract_status_code_from_attribute(self) -> None:
        exc = Exception("fail")
        exc.status_code = 429  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 429

    def test_extract_status_code_from_response(self) -> None:
        exc = Exception("fail")
        exc.response = MagicMock(status_code=503)  # type: ignore[attr-defined]
        assert _extract_status_code(exc) == 503

    def test_extract_status_code_from_string(self) -> None:
        exc = Exception("HTTP error 429: rate limited")
        assert _extract_status_code(exc) == 429

    def test_extract_status_code_returns_none_for_unknown(self) -> None:
        exc = Exception("something went wrong")
        assert _extract_status_code(exc) is None

    def test_is_timeout_for_timeout_error(self) -> None:
        assert _is_timeout(TimeoutError("timed out")) is True

    def test_is_timeout_for_string_match(self) -> None:
        assert _is_timeout(Exception("Connection timeout after 30s")) is True

    def test_is_timeout_for_non_timeout(self) -> None:
        assert _is_timeout(Exception("general error")) is False

    def test_is_quota_error_with_status(self) -> None:
        exc = Exception("fail")
        exc.status_code = 429  # type: ignore[attr-defined]
        assert _is_quota_error(exc) is True

    def test_is_quota_error_with_string(self) -> None:
        assert _is_quota_error(Exception("Resource exhausted")) is True
        assert _is_quota_error(Exception("quota exceeded")) is True
        assert _is_quota_error(Exception("rate limit reached")) is True

    def test_is_quota_error_for_non_quota(self) -> None:
        assert _is_quota_error(Exception("not found")) is False

    def test_is_auth_error(self) -> None:
        exc = Exception("fail")
        exc.status_code = 401  # type: ignore[attr-defined]
        assert _is_auth_error(exc) is True

        exc2 = Exception("fail")
        exc2.status_code = 403  # type: ignore[attr-defined]
        assert _is_auth_error(exc2) is True

    def test_is_auth_error_for_non_auth(self) -> None:
        exc = Exception("fail")
        exc.status_code = 200  # type: ignore[attr-defined]
        assert _is_auth_error(exc) is False

    def test_is_claude_overloaded(self) -> None:
        exc = Exception("fail")
        exc.status_code = 529  # type: ignore[attr-defined]
        assert _is_claude_overloaded(exc) is True

    def test_is_claude_overloaded_string(self) -> None:
        assert _is_claude_overloaded(
            Exception("529 overloaded: Claude is busy")
        ) is True

    def test_is_server_error(self) -> None:
        for code in (500, 502, 503, 504):
            exc = Exception("fail")
            exc.status_code = code  # type: ignore[attr-defined]
            assert _is_server_error(exc) is True, f"Expected True for {code}"

    def test_is_server_error_excludes_529(self) -> None:
        exc = Exception("fail")
        exc.status_code = 529  # type: ignore[attr-defined]
        assert _is_server_error(exc) is False


class TestClassifyError:
    """Tests for the error classification strategy mapper."""

    def test_auth_error_no_retry(self) -> None:
        exc = Exception("fail")
        exc.status_code = 401  # type: ignore[attr-defined]
        strategy = _classify_error(exc)
        assert strategy.should_retry is False
        assert strategy.alert is True

    def test_claude_overloaded_retry_with_long_delay(self) -> None:
        exc = Exception("fail")
        exc.status_code = 529  # type: ignore[attr-defined]
        strategy = _classify_error(exc)
        assert strategy.should_retry is True
        assert strategy.delay_seconds == 30.0
        assert strategy.max_attempts == 5

    def test_quota_error_retry_with_60s_delay(self) -> None:
        exc = Exception("fail")
        exc.status_code = 429  # type: ignore[attr-defined]
        strategy = _classify_error(exc)
        assert strategy.should_retry is True
        assert strategy.delay_seconds == 60.0

    def test_timeout_retry_twice(self) -> None:
        strategy = _classify_error(TimeoutError("timed out"))
        assert strategy.should_retry is True
        assert strategy.max_attempts == 2

    def test_server_error_retry(self) -> None:
        exc = Exception("fail")
        exc.status_code = 503  # type: ignore[attr-defined]
        strategy = _classify_error(exc)
        assert strategy.should_retry is True
        assert strategy.delay_seconds == 2.0

    def test_generic_error_retry(self) -> None:
        strategy = _classify_error(Exception("unknown"))
        assert strategy.should_retry is True


# ---------------------------------------------------------------------------
# Circuit Breaker tests
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    """Tests for the _CircuitMetrics circuit breaker."""

    def _make_circuit(
        self,
        threshold: int = 3,
        window: float = 60.0,
        cooldown: float = 1.0,
        success_threshold: int = 2,
    ) -> _CircuitMetrics:
        return _CircuitMetrics(
            failure_window_seconds=window,
            failure_threshold=threshold,
            cooldown_seconds=cooldown,
            success_threshold=success_threshold,
        )

    def test_starts_closed(self) -> None:
        c = self._make_circuit()
        assert c.state == CircuitState.CLOSED

    def test_stays_closed_below_threshold(self) -> None:
        c = self._make_circuit(threshold=5)
        for _ in range(4):
            c.record_failure()
        assert c.state == CircuitState.CLOSED

    def test_opens_at_threshold(self) -> None:
        c = self._make_circuit(threshold=3)
        for _ in range(3):
            c.record_failure()
        assert c.state == CircuitState.OPEN

    def test_transitions_to_half_open_after_cooldown(self) -> None:
        c = self._make_circuit(threshold=3, cooldown=0.1)
        for _ in range(3):
            c.record_failure()
        assert c.state == CircuitState.OPEN
        time.sleep(0.15)
        assert c.state == CircuitState.HALF_OPEN

    def test_closes_after_success_threshold_in_half_open(self) -> None:
        c = self._make_circuit(threshold=3, cooldown=0.05, success_threshold=2)
        for _ in range(3):
            c.record_failure()
        time.sleep(0.1)
        assert c.state == CircuitState.HALF_OPEN

        c.record_success()
        assert c.state == CircuitState.HALF_OPEN  # 1 success, need 2

        c.record_success()
        assert c.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self) -> None:
        c = self._make_circuit(threshold=3, cooldown=0.05)
        for _ in range(3):
            c.record_failure()
        time.sleep(0.1)
        assert c.state == CircuitState.HALF_OPEN

        c.record_failure()
        assert c.state == CircuitState.OPEN

    def test_prunes_old_failures(self) -> None:
        c = self._make_circuit(threshold=3, window=0.1)
        c.record_failure()
        c.record_failure()
        time.sleep(0.15)
        c.record_failure()
        # Only 1 failure in window, should stay closed
        assert c.state == CircuitState.CLOSED

    def test_reset(self) -> None:
        c = self._make_circuit(threshold=2)
        c.record_failure()
        c.record_failure()
        assert c.state == CircuitState.OPEN
        c.reset()
        assert c.state == CircuitState.CLOSED

    def test_thread_safety(self) -> None:
        """Verify no deadlocks or crashes under concurrent access."""
        c = self._make_circuit(threshold=1000, window=5.0)
        errors: list[Exception] = []

        def record_many() -> None:
            try:
                for _ in range(50):
                    c.record_failure()
                    c.record_success()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # No crash — circuit state is valid (may or may not be closed
        # depending on interleaving, but must not deadlock)
        assert c.state in (CircuitState.CLOSED, CircuitState.OPEN)


class TestGetCircuitStatus:
    """Tests for the get_circuit_status function."""

    def test_returns_all_known_services(self) -> None:
        status = get_circuit_status()
        for svc in ("gemini", "claude", "zoom", "drive", "whatsapp", "pinecone"):
            assert svc in status
            assert "state" in status[svc]
            assert "recent_failures" in status[svc]

    def test_open_circuit_includes_retry_after(self) -> None:
        circuit = _get_circuit("gemini")
        # Force open
        for _ in range(15):
            circuit.record_failure()
        status = get_circuit_status()
        assert status["gemini"]["state"] == "open"
        assert "retry_after_seconds" in status["gemini"]
        # Clean up
        circuit.reset()


# ---------------------------------------------------------------------------
# @resilient_api_call decorator tests
# ---------------------------------------------------------------------------


class TestResilientApiCallDecorator:
    """Tests for the main decorator."""

    def setup_method(self) -> None:
        """Reset circuits before each test."""
        for svc in ("gemini", "claude", "zoom", "drive", "whatsapp", "pinecone", "test_svc"):
            _get_circuit(svc).reset()

    def test_success_on_first_attempt(self) -> None:
        @resilient_api_call(service="test_svc", operation="test_op")
        def good_call() -> str:
            return "ok"

        assert good_call() == "ok"

    @patch("tools.core.api_resilience.time.sleep")
    def test_retries_on_server_error(self, mock_sleep: MagicMock) -> None:
        call_count = 0

        @resilient_api_call(
            service="test_svc", operation="retry_test",
            max_attempts=3, backoff_base=0.01,
        )
        def flaky_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                exc = Exception("server error")
                exc.status_code = 503  # type: ignore[attr-defined]
                raise exc
            return "recovered"

        assert flaky_call() == "recovered"
        assert call_count == 3

    def test_no_retry_on_auth_error(self) -> None:
        call_count = 0

        @resilient_api_call(
            service="test_svc", operation="auth_test",
            max_attempts=3, alert_on_auth=False,
        )
        def auth_fail() -> str:
            nonlocal call_count
            call_count += 1
            exc = Exception("unauthorized")
            exc.status_code = 401  # type: ignore[attr-defined]
            raise exc

        with pytest.raises(Exception, match="unauthorized"):
            auth_fail()
        assert call_count == 1  # No retries

    @patch("tools.core.api_resilience._alert_auth_failure")
    def test_alerts_on_auth_error(self, mock_alert: MagicMock) -> None:
        @resilient_api_call(
            service="test_svc", operation="auth_alert",
            max_attempts=1, alert_on_auth=True,
        )
        def auth_fail() -> str:
            exc = Exception("forbidden")
            exc.status_code = 403  # type: ignore[attr-defined]
            raise exc

        with pytest.raises(Exception):
            auth_fail()
        mock_alert.assert_called_once()

    @patch("tools.core.api_resilience.time.sleep")
    def test_raises_after_max_attempts(self, mock_sleep: MagicMock) -> None:
        @resilient_api_call(
            service="test_svc", operation="exhaust_test",
            max_attempts=2, backoff_base=0.01,
        )
        def always_fail() -> str:
            exc = Exception("server down")
            exc.status_code = 500  # type: ignore[attr-defined]
            raise exc

        with pytest.raises(Exception, match="server down"):
            always_fail()

    def test_circuit_open_raises_immediately(self) -> None:
        circuit = _get_circuit("test_svc")
        # Force open
        for _ in range(15):
            circuit.record_failure()
        assert circuit.state == CircuitState.OPEN

        @resilient_api_call(service="test_svc", operation="blocked")
        def should_not_run() -> str:
            return "should not reach"

        with pytest.raises(CircuitOpenError):
            should_not_run()

    @patch("tools.core.api_resilience.time.sleep")
    def test_gemini_quota_fallback_injects_use_free(self, mock_sleep: MagicMock) -> None:
        call_log: list[bool] = []

        @resilient_api_call(
            service="gemini", operation="transcribe",
            max_attempts=3, backoff_base=0.01,
            gemini_quota_fallback=True,
        )
        def gemini_call(use_free: bool = False) -> str:
            call_log.append(use_free)
            if not use_free:
                raise Exception("429 Resource exhausted: quota")
            return "success_free"

        result = gemini_call()
        assert result == "success_free"
        # First call with use_free=False, then retry with use_free=True
        assert call_log[0] is False
        assert call_log[-1] is True

    @patch("tools.core.api_resilience.time.sleep")
    def test_claude_overloaded_retries_with_extended_attempts(
        self, mock_sleep: MagicMock
    ) -> None:
        """529 errors should allow up to 5 attempts (extended from default 2)."""
        call_count = 0

        @resilient_api_call(
            service="claude", operation="reasoning",
            max_attempts=2, backoff_base=0.01,
        )
        def claude_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                exc = Exception("overloaded")
                exc.status_code = 529  # type: ignore[attr-defined]
                raise exc
            return "done"

        # max_attempts=2 but 529 bumps effective_max to 5
        result = claude_call()
        assert result == "done"
        assert call_count == 4
        # Verify sleep was called with 30s delay for 529
        assert any(
            args[0] == 30.0
            for args, _ in mock_sleep.call_args_list
        )

    @patch("tools.core.api_resilience.time.sleep")
    def test_timeout_retries_once_then_fails(self, mock_sleep: MagicMock) -> None:
        call_count = 0

        @resilient_api_call(
            service="test_svc", operation="timeout_test",
            max_attempts=2, backoff_base=0.01,
        )
        def timeout_call() -> str:
            nonlocal call_count
            call_count += 1
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError):
            timeout_call()
        assert call_count == 2  # original + 1 retry

    def test_records_circuit_breaker_on_failure(self) -> None:
        circuit = _get_circuit("test_svc")
        circuit.reset()

        @resilient_api_call(
            service="test_svc", operation="fail_record",
            max_attempts=1, backoff_base=0.01,
        )
        def single_fail() -> str:
            exc = Exception("boom")
            exc.status_code = 500  # type: ignore[attr-defined]
            raise exc

        with pytest.raises(Exception):
            single_fail()

        with circuit._lock:
            assert len(circuit._failure_timestamps) >= 1

    def test_records_circuit_breaker_on_success(self) -> None:
        circuit = _get_circuit("test_svc")
        circuit.reset()
        # Put circuit in half-open state
        for _ in range(15):
            circuit.record_failure()
        circuit._state = CircuitState.HALF_OPEN
        circuit._half_open_successes = 0

        @resilient_api_call(service="test_svc", operation="success_record")
        def succeed() -> str:
            return "ok"

        succeed()
        assert circuit._half_open_successes >= 1
