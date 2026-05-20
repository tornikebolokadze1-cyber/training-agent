"""Shared test configuration — centralised module stubs.

This conftest runs BEFORE any test file in tools/tests/.  It registers
lightweight stubs for every heavy optional dependency so that project
modules can be imported without installing the full dependency tree or
touching real API endpoints.

**Rules:**
- Every attribute used by ANY test file must be declared here.
- The _stub_module helper FORCE-registers stubs (overwriting any real
  module that was imported earlier, e.g. by the root conftest).
- test_whatsapp_assistant.py pops the tools.services.whatsapp_assistant stub
  and imports the real module — that is expected and safe.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Project root on sys.path
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Stub helper — always creates/overwrites to avoid conflicts with real
# packages that may have been imported by the root conftest.
# ---------------------------------------------------------------------------

_STUBS: dict[str, types.ModuleType] = {}


# ---------------------------------------------------------------------------
# Prometheus idempotency patch (US-025 — test pollution fix, 2026-05-14)
#
# Multiple test files pop `tools.app.server` from sys.modules and re-import
# it to get a fresh app instance with real FastAPI. Each re-import calls
# `prometheus_client.Counter("http_requests_total", ...)` which tries to
# register in the GLOBAL `prometheus_client.REGISTRY`. The second
# registration raises `ValueError: Duplicated timeseries`.
#
# The production `_safe_counter` helper in server.py catches the first
# ValueError but its fallback path re-calls `Counter(name, ...)` which
# re-raises (the lookup-by-name in REGISTRY misses because Counter stores
# `_name` differently from what `_collector_to_names` returns).
#
# Patching production code is out of scope for the US-025 audit. Instead,
# we wrap `prometheus_client.Counter` and `Histogram` at TEST conftest level
# to transparently return the EXISTING collector on duplicate-registration.
# ---------------------------------------------------------------------------
try:
    import prometheus_client as _prom

    _real_Counter = _prom.Counter
    _real_Histogram = _prom.Histogram

    def _find_existing(name: str):
        registry = _prom.REGISTRY
        for collector in list(registry._collector_to_names.keys()):  # type: ignore[attr-defined]
            collector_names = registry._collector_to_names.get(collector, set())  # type: ignore[attr-defined]
            if name in collector_names or f"{name}_total" in collector_names:
                return collector
        return None

    def _make_idempotent(real_cls):
        def _factory(name, documentation, labelnames=(), *args, **kwargs):
            try:
                return real_cls(name, documentation, labelnames, *args, **kwargs)
            except ValueError:
                existing = _find_existing(name)
                if existing is not None:
                    return existing
                # Last resort — unregister duplicates and retry once.
                for collector in list(_prom.REGISTRY._collector_to_names.keys()):  # type: ignore[attr-defined]
                    try:
                        names = _prom.REGISTRY._collector_to_names.get(collector, set())  # type: ignore[attr-defined]
                        if name in names or any(n.startswith(name) for n in names):
                            _prom.REGISTRY.unregister(collector)
                    except Exception:
                        pass
                return real_cls(name, documentation, labelnames, *args, **kwargs)
        return _factory

    _prom.Counter = _make_idempotent(_real_Counter)  # type: ignore[assignment]
    _prom.Histogram = _make_idempotent(_real_Histogram)  # type: ignore[assignment]
except ImportError:
    pass


def _stub_module(name: str) -> types.ModuleType:
    """Create or retrieve a stub module, ensuring it is in sys.modules."""
    if name in _STUBS:
        return _STUBS[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    _STUBS[name] = mod
    return mod


# ===================================================================
# google namespace
# ===================================================================
_google = _stub_module("google")

_google_genai = _stub_module("google.genai")
_google_genai.Client = MagicMock
_google_genai_types = _stub_module("google.genai.types")
_google_genai_types.GenerateContentConfig = object
_google_genai_types.UploadFileConfig = MagicMock
_google.genai = _google_genai

_google_oauth2 = _stub_module("google.oauth2")
_google_oauth2_creds = _stub_module("google.oauth2.credentials")
_google_oauth2_creds.Credentials = MagicMock
_google_oauth2_svc = _stub_module("google.oauth2.service_account")
_google_oauth2_svc.Credentials = MagicMock
_google.oauth2 = _google_oauth2

_google_auth = _stub_module("google.auth")
_google_auth_exceptions = _stub_module("google.auth.exceptions")
_google_auth_exceptions.TransportError = type("TransportError", (Exception,), {})
_google_auth.exceptions = _google_auth_exceptions
_google_auth_transport = _stub_module("google.auth.transport")
_google_auth_transport_requests = _stub_module("google.auth.transport.requests")
_google_auth_transport_requests.Request = MagicMock
_google.auth = _google_auth

_stub_module("google_auth_oauthlib")
_google_auth_oauthlib_flow = _stub_module("google_auth_oauthlib.flow")
_google_auth_oauthlib_flow.InstalledAppFlow = MagicMock

# ===================================================================
# googleapiclient
# ===================================================================
_googleapiclient_discovery = _stub_module("googleapiclient.discovery")
_googleapiclient_discovery.build = MagicMock

_googleapiclient_errors = _stub_module("googleapiclient.errors")
_googleapiclient_errors.HttpError = type("HttpError", (Exception,), {})

_googleapiclient_http = _stub_module("googleapiclient.http")
_googleapiclient_http.MediaFileUpload = MagicMock
_googleapiclient_http.MediaIoBaseDownload = MagicMock
_googleapiclient_http.MediaIoBaseUpload = MagicMock

_googleapiclient = _stub_module("googleapiclient")
_googleapiclient.discovery = _googleapiclient_discovery
_googleapiclient.errors = _googleapiclient_errors
_googleapiclient.http = _googleapiclient_http

# ===================================================================
# pinecone — kept for any lingering imports during the Qdrant migration
# ===================================================================
_pinecone = _stub_module("pinecone")

def _make_pinecone_client(*args, **kwargs):
    """Return a mock Pinecone client with JSON-serializable dict returns."""
    client = MagicMock()
    index = MagicMock()
    index.describe_index_stats.return_value = {
        "total_vector_count": 0,
        "dimension": 3072,
        "index_fullness": 0.0,
        "namespaces": {},
    }
    index.query.return_value = {"matches": []}
    index.upsert.return_value = {"upserted_count": 0}
    client.Index.return_value = index
    return client

_pinecone.Pinecone = _make_pinecone_client
_pinecone.ServerlessSpec = MagicMock

# ===================================================================
# qdrant-client (replaces Pinecone, 2026-05-20)
#
# We mirror the official qdrant-client surface used by knowledge_indexer:
#   QdrantClient, http.models.{VectorParams, Distance, Filter,
#   FieldCondition, MatchValue, FilterSelector, PointStruct}
# Each model is a thin object that accepts the kwargs used in production
# code so isinstance() checks and attribute access work in tests.
# ===================================================================
_qdrant = _stub_module("qdrant_client")


class _QdrantCountResult:
    def __init__(self, count: int = 0) -> None:
        self.count = count


class _QdrantCollectionInfo:
    def __init__(self, points_count: int = 0) -> None:
        self.points_count = points_count


class _QdrantCollectionsList:
    def __init__(self) -> None:
        self.collections = []


def _make_qdrant_client(*args, **kwargs):
    """Return a mock QdrantClient with sensible default responses."""
    client = MagicMock()
    client.get_collections.return_value = _QdrantCollectionsList()
    client.get_collection.return_value = _QdrantCollectionInfo(points_count=0)
    client.count.return_value = _QdrantCountResult(count=0)
    client.upsert.return_value = MagicMock(status="completed")
    client.delete.return_value = MagicMock(status="completed")
    client.create_collection.return_value = True
    client.query_points.return_value = MagicMock(points=[])
    return client


_qdrant.QdrantClient = _make_qdrant_client

# qdrant_client.http and qdrant_client.http.models
_qdrant_http = _stub_module("qdrant_client.http")
_qdrant_http_models = _stub_module("qdrant_client.http.models")


class _SimpleModel:
    """Generic stand-in: stores kwargs as attributes so production code
    can introspect ``payload.score`` / ``.points`` / etc."""

    def __init__(self, *args, **kwargs):
        # positional → store as 'value' / 'vector' / 'id' depending on order
        for i, a in enumerate(args):
            setattr(self, f"_arg{i}", a)
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Distance:
    COSINE = "Cosine"
    EUCLID = "Euclid"
    DOT = "Dot"


_qdrant_http_models.VectorParams = _SimpleModel
_qdrant_http_models.Distance = _Distance
_qdrant_http_models.Filter = _SimpleModel
_qdrant_http_models.FieldCondition = _SimpleModel
_qdrant_http_models.MatchValue = _SimpleModel
_qdrant_http_models.FilterSelector = _SimpleModel
_qdrant_http_models.PointStruct = _SimpleModel
_qdrant_http_models.ScoredPoint = _SimpleModel
_qdrant_http_models.QueryResponse = _SimpleModel
_qdrant_http.models = _qdrant_http_models
_qdrant.http = _qdrant_http
_qdrant.models = _qdrant_http_models

# ===================================================================
# anthropic — full exception hierarchy so isinstance() works
# ===================================================================
_APIError = type("APIError", (Exception,), {})
_RateLimitError = type("RateLimitError", (_APIError,), {})
_anthropic = _stub_module("anthropic")
_anthropic.Anthropic = MagicMock
_anthropic.APIError = _APIError
_anthropic.RateLimitError = _RateLimitError

# ===================================================================
# httpx
# ===================================================================
_httpx = _stub_module("httpx")
_httpx.Client = MagicMock
_httpx.AsyncClient = MagicMock
_httpx.Timeout = MagicMock
_httpx.TransportError = type("TransportError", (Exception,), {})
_httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
_httpx.TimeoutException = type("TimeoutException", (_httpx.TransportError,), {})
_httpx.RequestError = type("RequestError", (Exception,), {})

# ===================================================================
# fastapi — including middleware and responses submodules
# ===================================================================
_fastapi = _stub_module("fastapi")
_fastapi.FastAPI = MagicMock
_fastapi.Header = MagicMock(return_value=None)
_fastapi.HTTPException = type(
    "HTTPException",
    (Exception,),
    {"__init__": lambda self, status_code=0, detail="": None},
)
_fastapi.Request = MagicMock
_fastapi.BackgroundTasks = MagicMock
_fastapi.APIRouter = MagicMock

_fastapi_responses = _stub_module("fastapi.responses")
_fastapi_responses.JSONResponse = MagicMock
_fastapi_responses.HTMLResponse = MagicMock

_fastapi_middleware = _stub_module("fastapi.middleware")
_fastapi_middleware_trustedhost = _stub_module("fastapi.middleware.trustedhost")
_fastapi_middleware_trustedhost.TrustedHostMiddleware = MagicMock
_fastapi_middleware_cors = _stub_module("fastapi.middleware.cors")
_fastapi_middleware_cors.CORSMiddleware = MagicMock

# ===================================================================
# slowapi
# ===================================================================
_slowapi = _stub_module("slowapi")
_slowapi.Limiter = MagicMock
_slowapi._rate_limit_exceeded_handler = MagicMock

_slowapi_errors = _stub_module("slowapi.errors")
_slowapi_errors.RateLimitExceeded = type("RateLimitExceeded", (Exception,), {})

_slowapi_util = _stub_module("slowapi.util")
_slowapi_util.get_remote_address = MagicMock

# ===================================================================
# pydantic
# ===================================================================
_pydantic = _stub_module("pydantic")


class _BaseModel:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def model_dump(self):
        return self.__dict__


_pydantic.BaseModel = _BaseModel


def _field_validator(*args, **kwargs):
    """No-op decorator factory standing in for pydantic.field_validator.

    Real pydantic v2 returns a decorator that registers a validator on the
    BaseModel. The stub just passes the function through unchanged so any
    test importing a module that uses @field_validator can collect.
    """
    def _decorator(fn):
        return fn
    return _decorator


_pydantic.field_validator = _field_validator
_pydantic.ValidationError = type("ValidationError", (Exception,), {})


def _ConfigDict(**kwargs):
    """Stub for pydantic v2 ConfigDict — production code uses it as a
    typed dict for model configuration. Tests don't enforce the config,
    so the stub returns the kwargs dict and is a no-op at validation
    time. Added to unblock imports that pin pydantic v2 ConfigDict at
    module load (e.g. tools/app/server.py).
    """
    return kwargs


_pydantic.ConfigDict = _ConfigDict

# ===================================================================
# dotenv
# ===================================================================
_dotenv = _stub_module("dotenv")
_dotenv.load_dotenv = lambda *a, **kw: None
_dotenv.dotenv_values = lambda *a, **kw: {}

# ===================================================================
# apscheduler (used by scheduler.py)
# ===================================================================
_apscheduler = _stub_module("apscheduler")
_apscheduler_executors = _stub_module("apscheduler.executors")
_apscheduler_executors_asyncio = _stub_module("apscheduler.executors.asyncio")
_apscheduler_executors_asyncio.AsyncIOExecutor = MagicMock
_apscheduler_executors_pool = _stub_module("apscheduler.executors.pool")
_apscheduler_executors_pool.ThreadPoolExecutor = MagicMock
_apscheduler_schedulers = _stub_module("apscheduler.schedulers")
_apscheduler_schedulers_asyncio = _stub_module("apscheduler.schedulers.asyncio")

_AsyncIOScheduler = MagicMock()
_AsyncIOScheduler.return_value.get_jobs.return_value = []
_apscheduler_schedulers_asyncio.AsyncIOScheduler = _AsyncIOScheduler

_apscheduler_triggers = _stub_module("apscheduler.triggers")
_apscheduler_triggers_cron = _stub_module("apscheduler.triggers.cron")
_apscheduler_triggers_cron.CronTrigger = MagicMock

# ===================================================================
# uvicorn (used by orchestrator.py and server.py)
# ===================================================================
_uvicorn = _stub_module("uvicorn")
_uvicorn.Config = MagicMock
_uvicorn.Server = MagicMock

# ===================================================================
# Internal module stubs — only whatsapp_assistant needs a NoOp stub
# because its constructor validates API keys at module level (called
# from server.py line 145).  All other internal modules import safely
# with the external stubs above.
#
# test_whatsapp_assistant.py pops this stub and imports the real one.
# ===================================================================


class _NoOpAssistant:
    async def handle_message(self, *a, **kw):
        return None


class _IncomingMessage:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


_wa_assistant_mod = _stub_module("tools.services.whatsapp_assistant")
_wa_assistant_mod.WhatsAppAssistant = _NoOpAssistant
_wa_assistant_mod.IncomingMessage = _IncomingMessage


# ===================================================================
# Global cache-clearing fixture — prevents shared state bleed between
# tests.  Clears all module-level caches that could cause flaky tests
# if a previous test left dirty state.
# ===================================================================
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_caches() -> None:  # type: ignore[misc]
    """Reset all module-level caches before each test."""
    # Snapshot httpx module state so we can detect pollution from test_server_new.py
    _httpx_mod = sys.modules.get("httpx")
    _httpx_client_before = getattr(_httpx_mod, "Client", None) if _httpx_mod else None
    yield
    # Post-test cleanup: reset ALL circuit breakers to prevent cross-test pollution
    try:
        from tools.core.api_resilience import _circuits
        for circuit in _circuits.values():
            circuit.reset()
    except ImportError:
        pass
    # Post-test cleanup: clear caches that might bleed between tests
    for mod_name, attrs in [
        ("tools.integrations.gdrive_manager", [
            "_token_path_cache", "_drive_service_cache", "_docs_service_cache",
        ]),
        ("tools.integrations.knowledge_indexer", ["_pinecone_index_cache", "_qdrant_client_cache"]),
        ("tools.integrations.zoom_manager", ["_token_cache"]),
    ]:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for attr in attrs:
            if hasattr(mod, attr):
                val = getattr(mod, attr)
                if isinstance(val, dict):
                    val.clear()
                else:
                    setattr(mod, attr, None)

    # Restore httpx stubs if they were overwritten by test_server.py/test_server_new.py
    # which pop stubs and import real httpx for TestClient. This ensures subsequent
    # tests still get mock httpx.
    _httpx_now = sys.modules.get("httpx")
    if _httpx_now is not None and not isinstance(getattr(_httpx_now, "Client", None), type(MagicMock)):
        # Real httpx is loaded — re-apply stubs
        _httpx_now.Client = MagicMock
        _httpx_now.AsyncClient = MagicMock
        _httpx_now.Timeout = MagicMock
        _httpx_now.TransportError = type("TransportError", (Exception,), {})
        _httpx_now.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
        _httpx_now.TimeoutException = type("TimeoutException", (Exception,), {})
        _httpx_now.RequestError = type("RequestError", (Exception,), {})

    # Issue #52 — in-process stub-restore was attempted here but produced
    # cross-file pollution worse than the original symptom (real fastapi
    # leaked into later stubbed tests as expected; restoring stubs broke
    # the next test in any file that still needed real modules in-flight).
    # The canonical fix is the per-file runner at scripts/run_tests_isolated.sh
    # which sidesteps the problem with process boundaries.  Full
    # in-process fix (refactoring the four stub-popping test files to
    # restore at module-teardown instead of module-load) is deferred.


# ---------------------------------------------------------------------------
# Budget auto-mock — prevent production scores.db state from breaking tests
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _mock_daily_budget(monkeypatch):
    """Mock cost_tracker budget checks so tests don't depend on real scores.db state.

    Without this, tests that run transcribe_and_index can fail with
    'Daily cost limit reached' when the real database has accumulated
    production costs from launchd-driven lecture processing.
    """
    try:
        import tools.core.cost_tracker as _ct
        monkeypatch.setattr(_ct, "check_daily_budget", lambda: (True, 100.0))
        monkeypatch.setattr(_ct, "check_lecture_budget", lambda key: (True, 100.0))
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _cleanup_stale_state_files():
    """Remove real pipeline_state JSONs before each test.

    Production launchd lectures can leave FAILED state files in .tmp/
    which break transcribe_and_index tests that assume a clean slate.
    We delete only pipeline_state_g*_l*.json — other .tmp/ files (checkpoints)
    are left alone so test_pipeline_state_hardened continues to work.

    Also wipes the delivery_tracker.json so cross-retry idempotency state
    from production / earlier tests cannot bleed into transcribe_and_index
    test cases (each test gets a clean slate for delivery tracking).
    """
    from pathlib import Path
    import glob
    project_tmp = Path(__file__).parent.parent.parent / ".tmp"
    if project_tmp.exists():
        for f in glob.glob(str(project_tmp / "pipeline_state_g*_l*.json")):
            try:
                Path(f).unlink()
            except OSError:
                pass
        # Wipe delivery_tracker.json so previous runs cannot leak idempotency
        # markers into a fresh test. Tests that need a pre-populated tracker
        # should call record_delivery() explicitly in their setup.
        tracker = project_tmp / "delivery_tracker.json"
        if tracker.exists():
            try:
                tracker.unlink()
            except OSError:
                pass
    yield


@pytest.fixture(autouse=True)
def _mock_zoom_token(request, monkeypatch):
    """Prevent test_server's real httpx from hitting real Zoom OAuth.

    When test_server.py pops httpx stubs to use TestClient, any code path
    that calls zoom_manager.get_access_token() with fake credentials
    (ZOOM_CLIENT_ID=test in CI) produces HTTP 400 warnings and can cause
    MagicMock leaks into JSON bodies.

    Skipped for TestGetAccessToken which tests the real function directly.
    """
    # TestGetAccessToken tests the real get_access_token — don't mock it there.
    cls = request.node.cls
    if cls is not None and cls.__name__ == "TestGetAccessToken":
        return
    try:
        import tools.integrations.zoom_manager as _zm
        monkeypatch.setattr(_zm, "get_access_token", lambda: "fake-test-token")
    except ImportError:
        pass
