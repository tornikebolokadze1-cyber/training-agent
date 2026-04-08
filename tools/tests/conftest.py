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
# pinecone
# ===================================================================
_pinecone = _stub_module("pinecone")
_pinecone.Pinecone = MagicMock
_pinecone.ServerlessSpec = MagicMock

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
        ("tools.integrations.knowledge_indexer", ["_pinecone_index_cache"]),
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
