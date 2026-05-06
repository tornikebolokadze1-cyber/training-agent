"""Unit tests for tools.integrations.mem0_memory.

Covers:
- is_enabled() False when required env vars are missing
- is_enabled() True when all required vars are set
- All public functions return empty/None without raising when disabled
- Correctly delegates add_memory / search_memory / get_all / delete_user
  to a mocked Mem0 client
- Wrapper catches Mem0 SDK exceptions and returns empty results
- Lazy initialisation: client is not created on import, only on first call

All Mem0 SDK calls are mocked — no external network connections are made.
Run with:
    pytest tools/tests/test_mem0_memory.py -v
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers to ensure a clean module state for each test
# ---------------------------------------------------------------------------

_REQUIRED_VARS = (
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "NEO4J_URL",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
)

_ALL_REQUIRED: dict[str, str] = {
    "QDRANT_URL": "https://qdrant.example.com:6333",
    "QDRANT_API_KEY": "qdrant-key-123",
    "NEO4J_URL": "neo4j+s://test.databases.neo4j.io",
    "NEO4J_USERNAME": "neo4j",
    "NEO4J_PASSWORD": "secret-neo4j-password",
}


@pytest.fixture(autouse=True)
def _reset_mem0_module():
    """Reload mem0_memory before each test to reset global singletons.

    This ensures that _client, _disabled_warned, and _client_lock are fresh
    for every test, preventing state bleed.
    """
    # Remove any cached module and any cached mem0 stub
    for key in list(sys.modules.keys()):
        if "mem0_memory" in key or key in ("mem0", "mem0.memory"):
            del sys.modules[key]
    yield
    # Clean up after test as well
    for key in list(sys.modules.keys()):
        if "mem0_memory" in key or key in ("mem0", "mem0.memory"):
            del sys.modules[key]


def _stub_mem0(mock_memory_instance: MagicMock) -> None:
    """Inject a stub ``mem0`` package into sys.modules.

    ``Memory.from_config()`` returns ``mock_memory_instance``.
    """
    mem0_mod = types.ModuleType("mem0")
    MockMemoryClass = MagicMock()
    MockMemoryClass.from_config.return_value = mock_memory_instance
    mem0_mod.Memory = MockMemoryClass  # type: ignore[attr-defined]
    sys.modules["mem0"] = mem0_mod


def _import_mem0_memory():
    """Import mem0_memory (fresh, after module reset)."""
    return importlib.import_module("tools.integrations.mem0_memory")


# ===========================================================================
# 1. is_enabled()
# ===========================================================================


class TestIsEnabled:
    """Tests for the is_enabled() configuration check."""

    def test_disabled_when_no_env_vars(self, monkeypatch):
        """is_enabled() returns False when all required vars are absent."""
        for var in _REQUIRED_VARS:
            monkeypatch.delenv(var, raising=False)
        mod = _import_mem0_memory()
        assert mod.is_enabled() is False

    def test_disabled_when_some_vars_missing(self, monkeypatch):
        """is_enabled() returns False when even one required var is missing."""
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)
        # Remove one required var
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        mod = _import_mem0_memory()
        assert mod.is_enabled() is False

    def test_disabled_when_var_is_empty_string(self, monkeypatch):
        """is_enabled() returns False when a var is present but empty."""
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)
        monkeypatch.setenv("QDRANT_API_KEY", "")
        mod = _import_mem0_memory()
        assert mod.is_enabled() is False

    def test_disabled_when_var_is_whitespace_only(self, monkeypatch):
        """is_enabled() returns False when a var is whitespace only."""
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)
        monkeypatch.setenv("NEO4J_USERNAME", "   ")
        mod = _import_mem0_memory()
        assert mod.is_enabled() is False

    def test_enabled_when_all_vars_present(self, monkeypatch):
        """is_enabled() returns True when all required vars have values."""
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)
        mod = _import_mem0_memory()
        assert mod.is_enabled() is True


# ===========================================================================
# 2. Graceful degradation — disabled path
# ===========================================================================


class TestGracefulDegradation:
    """All public functions return empty results without raising when disabled."""

    @pytest.fixture(autouse=True)
    def _no_env_vars(self, monkeypatch):
        for var in _REQUIRED_VARS:
            monkeypatch.delenv(var, raising=False)

    def test_add_memory_returns_empty_dict(self):
        mod = _import_mem0_memory()
        result = mod.add_memory("user123", "test message")
        assert result == {}

    def test_add_memory_does_not_raise(self):
        mod = _import_mem0_memory()
        # Should not raise regardless of input
        mod.add_memory("", "")
        mod.add_memory("user", "text", metadata={"k": "v"})

    def test_search_memory_returns_empty_list(self):
        mod = _import_mem0_memory()
        result = mod.search_memory("user123", "what did I ask yesterday?")
        assert result == []

    def test_search_memory_does_not_raise(self):
        mod = _import_mem0_memory()
        mod.search_memory("", "")
        mod.search_memory("user", "query", limit=10)

    def test_get_all_returns_empty_list(self):
        mod = _import_mem0_memory()
        result = mod.get_all("user123")
        assert result == []

    def test_get_all_does_not_raise(self):
        mod = _import_mem0_memory()
        mod.get_all("user123", limit=100)

    def test_delete_user_returns_none(self):
        mod = _import_mem0_memory()
        result = mod.delete_user("user123")
        assert result is None

    def test_delete_user_does_not_raise(self):
        mod = _import_mem0_memory()
        mod.delete_user("")
        mod.delete_user("user123")

    def test_disabled_warning_logged(self, caplog):
        """A WARNING is logged once when Mem0 is disabled."""
        import logging
        mod = _import_mem0_memory()
        with caplog.at_level(logging.WARNING, logger="tools.integrations.mem0_memory"):
            mod.add_memory("user", "text")
        assert any("disabled" in r.message.lower() for r in caplog.records)

    def test_disabled_warning_logged_only_once(self, caplog):
        """The disabled WARNING is emitted at most once per process lifetime."""
        import logging
        mod = _import_mem0_memory()
        with caplog.at_level(logging.WARNING, logger="tools.integrations.mem0_memory"):
            mod.add_memory("user", "text")
            mod.search_memory("user", "query")
            mod.get_all("user")
            mod.delete_user("user")
        warning_records = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "disabled" in r.message.lower()
        ]
        assert len(warning_records) == 1


# ===========================================================================
# 3. Enabled path — correct delegation to Mem0 client
# ===========================================================================


class TestEnabledDelegation:
    """Public functions correctly delegate to the Mem0 Memory client."""

    @pytest.fixture(autouse=True)
    def _set_env_vars(self, monkeypatch):
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)

    @pytest.fixture()
    def mock_client(self) -> MagicMock:
        """Return a fresh mock Mem0 client pre-registered in sys.modules."""
        client = MagicMock()
        _stub_mem0(client)
        return client

    # --- add_memory ---

    def test_add_memory_calls_client_add(self, mock_client):
        mock_client.add.return_value = {"results": [{"id": "mem1"}]}
        mod = _import_mem0_memory()
        result = mod.add_memory("user42", "hello from test")
        mock_client.add.assert_called_once()
        args, kwargs = mock_client.add.call_args
        # first positional arg must be a list of message dicts
        messages = args[0]
        assert isinstance(messages, list)
        assert messages[0]["content"] == "hello from test"
        assert kwargs.get("user_id") == "user42"
        assert result == {"results": [{"id": "mem1"}]}

    def test_add_memory_passes_metadata(self, mock_client):
        mock_client.add.return_value = {}
        mod = _import_mem0_memory()
        mod.add_memory("user42", "text", metadata={"group": 1})
        _, kwargs = mock_client.add.call_args
        assert kwargs.get("metadata") == {"group": 1}

    def test_add_memory_normalises_list_response(self, mock_client):
        """When Mem0 returns a list, wrapper wraps it in a dict."""
        mock_client.add.return_value = [{"id": "m1"}, {"id": "m2"}]
        mod = _import_mem0_memory()
        result = mod.add_memory("u", "t")
        assert isinstance(result, dict)
        assert "results" in result

    def test_add_memory_skips_metadata_when_none(self, mock_client):
        mock_client.add.return_value = {}
        mod = _import_mem0_memory()
        mod.add_memory("user42", "text")  # no metadata
        _, kwargs = mock_client.add.call_args
        assert "metadata" not in kwargs

    # --- search_memory ---

    def test_search_memory_calls_client_search(self, mock_client):
        mock_client.search.return_value = [{"memory": "previous question"}]
        mod = _import_mem0_memory()
        result = mod.search_memory("user42", "what did I ask?", limit=3)
        mock_client.search.assert_called_once_with(
            "what did I ask?", user_id="user42", limit=3
        )
        assert result == [{"memory": "previous question"}]

    def test_search_memory_normalises_dict_response(self, mock_client):
        """When Mem0 returns a dict, wrapper extracts the 'results' key."""
        mock_client.search.return_value = {
            "results": [{"memory": "item"}],
            "meta": {},
        }
        mod = _import_mem0_memory()
        result = mod.search_memory("u", "q")
        assert result == [{"memory": "item"}]

    def test_search_memory_returns_empty_for_blank_query(self, mock_client):
        mod = _import_mem0_memory()
        result = mod.search_memory("user42", "   ")
        mock_client.search.assert_not_called()
        assert result == []

    # --- get_all ---

    def test_get_all_calls_client_get_all(self, mock_client):
        memories = [{"memory": f"mem{i}"} for i in range(5)]
        mock_client.get_all.return_value = memories
        mod = _import_mem0_memory()
        result = mod.get_all("user42", limit=10)
        mock_client.get_all.assert_called_once_with(user_id="user42")
        assert result == memories

    def test_get_all_respects_limit(self, mock_client):
        memories = [{"memory": f"m{i}"} for i in range(20)]
        mock_client.get_all.return_value = memories
        mod = _import_mem0_memory()
        result = mod.get_all("user42", limit=5)
        assert len(result) == 5

    def test_get_all_normalises_dict_response(self, mock_client):
        mock_client.get_all.return_value = {
            "results": [{"memory": "one"}, {"memory": "two"}],
        }
        mod = _import_mem0_memory()
        result = mod.get_all("u")
        assert result == [{"memory": "one"}, {"memory": "two"}]

    # --- delete_user ---

    def test_delete_user_calls_client_delete_all(self, mock_client):
        mod = _import_mem0_memory()
        mod.delete_user("user42")
        mock_client.delete_all.assert_called_once_with(user_id="user42")

    def test_delete_user_returns_none(self, mock_client):
        mock_client.delete_all.return_value = None
        mod = _import_mem0_memory()
        result = mod.delete_user("user42")
        assert result is None


# ===========================================================================
# 4. Exception handling — SDK raises, wrapper catches
# ===========================================================================


class TestExceptionHandling:
    """Wrapper catches Mem0 SDK exceptions and returns empty results."""

    @pytest.fixture(autouse=True)
    def _set_env_vars(self, monkeypatch):
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)

    @pytest.fixture()
    def exploding_client(self) -> MagicMock:
        """Mock client where every method raises RuntimeError."""
        client = MagicMock()
        error = RuntimeError("Mem0 network timeout")
        client.add.side_effect = error
        client.search.side_effect = error
        client.get_all.side_effect = error
        client.delete_all.side_effect = error
        _stub_mem0(client)
        return client

    def test_add_memory_returns_empty_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        result = mod.add_memory("u", "text")
        assert result == {}

    def test_add_memory_does_not_raise_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        mod.add_memory("u", "text")  # must not propagate

    def test_search_memory_returns_empty_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        result = mod.search_memory("u", "query")
        assert result == []

    def test_search_memory_does_not_raise_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        mod.search_memory("u", "query")

    def test_get_all_returns_empty_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        result = mod.get_all("u")
        assert result == []

    def test_get_all_does_not_raise_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        mod.get_all("u")

    def test_delete_user_does_not_raise_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        mod.delete_user("u")  # must not propagate; logs warning instead

    def test_delete_user_returns_none_on_sdk_error(self, exploding_client):
        mod = _import_mem0_memory()
        result = mod.delete_user("u")
        assert result is None

    def test_sdk_error_is_logged(self, exploding_client, caplog):
        """Errors from the SDK surface as logged ERROR/WARNING, not exceptions."""
        import logging
        mod = _import_mem0_memory()
        with caplog.at_level(logging.ERROR, logger="tools.integrations.mem0_memory"):
            mod.add_memory("u", "text")
        assert any("Mem0" in r.message for r in caplog.records)


# ===========================================================================
# 5. Lazy initialisation — client not created until first API call
# ===========================================================================


class TestLazyInitialisation:
    """The Mem0 client is not instantiated at import time."""

    def test_import_does_not_create_client_when_disabled(self, monkeypatch):
        """Importing mem0_memory with missing vars should not touch Mem0 SDK."""
        for var in _REQUIRED_VARS:
            monkeypatch.delenv(var, raising=False)

        # Poison mem0 so that instantiation would raise if attempted
        poison_mod = types.ModuleType("mem0")
        def _explode(*a, **kw):
            raise AssertionError("Mem0 client should not be created when disabled")
        poison_class = MagicMock()
        poison_class.from_config.side_effect = _explode
        poison_mod.Memory = poison_class  # type: ignore[attr-defined]
        sys.modules["mem0"] = poison_mod

        # Import must not raise
        mod = _import_mem0_memory()
        assert mod._client is None  # still None — not initialised

    def test_client_created_on_first_api_call(self, monkeypatch):
        """_get_client() creates the Mem0 client on the first real API call."""
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)

        mock_client = MagicMock()
        mock_client.add.return_value = {}
        _stub_mem0(mock_client)

        mod = _import_mem0_memory()
        assert mod._client is None  # not yet created

        mod.add_memory("u", "text")
        assert mod._client is not None  # now created

    def test_client_reused_across_calls(self, monkeypatch):
        """Subsequent calls reuse the same client (no re-instantiation)."""
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)

        mock_instance = MagicMock()
        mock_instance.add.return_value = {}
        mock_instance.search.return_value = []

        mem0_mod = types.ModuleType("mem0")
        mock_class = MagicMock()
        mock_class.from_config.return_value = mock_instance
        mem0_mod.Memory = mock_class  # type: ignore[attr-defined]
        sys.modules["mem0"] = mem0_mod

        mod = _import_mem0_memory()
        mod.add_memory("u", "first call")
        mod.search_memory("u", "second call")

        # from_config should have been called only once
        assert mock_class.from_config.call_count == 1


# ===========================================================================
# 6. Client initialisation failure
# ===========================================================================


class TestClientInitFailure:
    """When Mem0 from_config() raises, all functions still return empty."""

    @pytest.fixture(autouse=True)
    def _set_env_vars(self, monkeypatch):
        for var, val in _ALL_REQUIRED.items():
            monkeypatch.setenv(var, val)

    @pytest.fixture(autouse=True)
    def _stub_broken_mem0(self):
        mem0_mod = types.ModuleType("mem0")
        bad_class = MagicMock()
        bad_class.from_config.side_effect = ConnectionError("Cannot reach Qdrant")
        mem0_mod.Memory = bad_class  # type: ignore[attr-defined]
        sys.modules["mem0"] = mem0_mod

    def test_add_memory_safe_after_init_failure(self):
        mod = _import_mem0_memory()
        result = mod.add_memory("u", "text")
        assert result == {}

    def test_search_memory_safe_after_init_failure(self):
        mod = _import_mem0_memory()
        result = mod.search_memory("u", "query")
        assert result == []

    def test_get_all_safe_after_init_failure(self):
        mod = _import_mem0_memory()
        result = mod.get_all("u")
        assert result == []

    def test_delete_user_safe_after_init_failure(self):
        mod = _import_mem0_memory()
        mod.delete_user("u")  # must not raise
