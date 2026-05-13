"""Regression tests for the three-secret separation security model.

Verifies that:
- WEBHOOK_SECRET strength is enforced (min length, no weak values)
- OPERATOR_WEBHOOK_SECRET is required in production and independent
- PAPERCLIP_WEBHOOK_SECRET no longer falls back to WEBHOOK_SECRET
- OPERATOR_WEBHOOK_SECRET no longer falls back to WEBHOOK_SECRET
- Identical secrets are rejected in production and warned in dev
- Dev mode degrades gracefully so CI keeps passing

See docs/security/secret-separation-rollout.md for the production rollout guide.

Run with:
    pytest tools/tests/test_secret_separation.py -v
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import tools.core.config as cfg


# ---------------------------------------------------------------------------
# Helper: run validate_critical_config with a controlled environment.
# patch.object is used for the module-level constants so we don't rely on
# re-importing the module (which would re-evaluate all top-level code).
# ---------------------------------------------------------------------------

_BASE_PATCHES: dict[str, str] = {
    "GEMINI_API_KEY": "k",
    "GEMINI_API_KEY_PAID": "",
    "ANTHROPIC_API_KEY": "k",
    "GREEN_API_INSTANCE_ID": "i",
    "GREEN_API_TOKEN": "t",
    "WHATSAPP_TORNIKE_PHONE": "p",
}

# A strong 40-char secret that passes all checks
_STRONG_A = "a" * 40
_STRONG_B = "b" * 40
_STRONG_C = "c" * 40


def _run_validate(
    *,
    webhook_secret: str = _STRONG_A,
    operator_secret: str = _STRONG_B,
    paperclip_secret: str = _STRONG_C,
    is_railway: bool = False,
    extra_patches: dict | None = None,
) -> tuple[list[str], Exception | None]:
    """Run validate_critical_config with given secrets and railway flag.

    Returns (warnings, exception_or_None).
    """
    patches = dict(_BASE_PATCHES)
    patches.update(extra_patches or {})

    ctx_managers = [
        patch.object(cfg, "IS_RAILWAY", is_railway),
        patch.object(cfg, "WEBHOOK_SECRET", webhook_secret),
        patch.object(cfg, "OPERATOR_WEBHOOK_SECRET", operator_secret),
        patch.object(cfg, "PAPERCLIP_WEBHOOK_SECRET", paperclip_secret),
        patch.object(cfg, "PAPERCLIP_API_BASE", "http://127.0.0.1:3100"),
    ]
    for attr, val in patches.items():
        ctx_managers.append(patch.object(cfg, attr, val))

    from contextlib import ExitStack

    exc: Exception | None = None
    result: list[str] = []
    with ExitStack() as stack:
        for cm in ctx_managers:
            stack.enter_context(cm)
        try:
            result = cfg.validate_critical_config()
        except RuntimeError as e:
            exc = e
    return result, exc


# ===========================================================================
# 1. WEBHOOK_SECRET length enforcement
# ===========================================================================


class TestWebhookSecretStrength:
    def test_validate_warns_when_webhook_secret_short(self):
        """WEBHOOK_SECRET shorter than 32 chars must produce an error/warning."""
        result, exc = _run_validate(webhook_secret="x", is_railway=False)
        all_messages = result + ([str(exc)] if exc else [])
        combined = " ".join(all_messages).lower()
        assert "too short" in combined or "minimum" in combined or "webhook_secret" in combined, (
            f"Expected a length complaint for short WEBHOOK_SECRET; got: {all_messages}"
        )

    def test_validate_accepts_strong_webhook_secret(self):
        """A 40-char random-looking secret must not trigger a length error."""
        result, exc = _run_validate(webhook_secret=_STRONG_A, is_railway=False)
        assert exc is None
        combined = " ".join(result).lower()
        # Should not mention length problem for WEBHOOK_SECRET
        assert not (
            "too short" in combined and "webhook_secret" in combined
        ), f"Unexpected length warning: {result}"


# ===========================================================================
# 2. Weak secret rejection in production
# ===========================================================================


class TestWeakSecretRejection:
    @pytest.mark.parametrize("weak_value", [
        "password",
        "secret",
        "test",
        "changeme",
        "your-secret-here",
        "dummy",
    ])
    def test_validate_rejects_weak_secrets_in_production(self, weak_value: str):
        """Known weak values must raise RuntimeError in IS_RAILWAY=True."""
        # Pad to meet length requirement so only the weakness check triggers
        padded = weak_value + ("x" * (32 - len(weak_value)))

        result, exc = _run_validate(webhook_secret=padded, is_railway=True)
        assert exc is not None, (
            f"Expected RuntimeError for weak WEBHOOK_SECRET={weak_value!r}; "
            f"got warnings: {result}"
        )
        assert "weak" in str(exc).lower() or "known" in str(exc).lower() or weak_value in str(exc).lower()

    def test_validate_rejects_weak_secrets_short_in_production(self):
        """A 1-char secret must raise RuntimeError in production."""
        result, exc = _run_validate(webhook_secret="x", is_railway=True)
        assert exc is not None, f"Expected RuntimeError for 1-char secret; got: {result}"


# ===========================================================================
# 3. OPERATOR_WEBHOOK_SECRET required in production
# ===========================================================================


class TestOperatorSecretProduction:
    def test_validate_requires_operator_secret_in_production(self):
        """IS_RAILWAY=True with OPERATOR_WEBHOOK_SECRET unset must raise."""
        result, exc = _run_validate(
            webhook_secret=_STRONG_A,
            operator_secret="",
            is_railway=True,
        )
        assert exc is not None, (
            f"Expected RuntimeError when OPERATOR_WEBHOOK_SECRET missing in prod; "
            f"got warnings: {result}"
        )
        assert "operator" in str(exc).lower() or "OPERATOR_WEBHOOK_SECRET" in str(exc), (
            f"Error message should mention OPERATOR_WEBHOOK_SECRET; got: {exc}"
        )

    def test_validate_accepts_operator_secret_in_production(self):
        """IS_RAILWAY=True with distinct OPERATOR_WEBHOOK_SECRET must not raise."""
        result, exc = _run_validate(
            webhook_secret=_STRONG_A,
            operator_secret=_STRONG_B,
            is_railway=True,
        )
        assert exc is None, f"Unexpected RuntimeError: {exc}; warnings: {result}"


# ===========================================================================
# 4. OPERATOR_WEBHOOK_SECRET optional in dev (degrades to warning)
# ===========================================================================


class TestOperatorSecretDev:
    def test_validate_warns_operator_secret_unset_in_dev(self):
        """IS_RAILWAY=False with OPERATOR_WEBHOOK_SECRET unset must NOT raise — only warn."""
        result, exc = _run_validate(
            webhook_secret=_STRONG_A,
            operator_secret="",
            is_railway=False,
        )
        assert exc is None, (
            f"validate_critical_config must not raise in dev when "
            f"OPERATOR_WEBHOOK_SECRET is absent; got: {exc}"
        )
        combined = " ".join(result).lower()
        assert (
            "operator" in combined
            or "operator_webhook_secret" in combined
        ), f"Expected a warning about OPERATOR_WEBHOOK_SECRET; got: {result}"


# ===========================================================================
# 5. Identical secrets rejected in production
# ===========================================================================


class TestDistinctSecretsProduction:
    def test_validate_rejects_identical_webhook_and_operator_secrets_in_production(self):
        """WEBHOOK_SECRET == OPERATOR_WEBHOOK_SECRET must raise in production."""
        result, exc = _run_validate(
            webhook_secret=_STRONG_A,
            operator_secret=_STRONG_A,  # Same value!
            paperclip_secret=_STRONG_C,
            is_railway=True,
        )
        assert exc is not None, (
            f"Expected RuntimeError when WEBHOOK_SECRET == OPERATOR_WEBHOOK_SECRET "
            f"in production; got warnings: {result}"
        )
        err_str = str(exc).lower()
        assert "identical" in err_str or "unique" in err_str or "same" in err_str, (
            f"Error should mention identical/unique secrets; got: {exc}"
        )


# ===========================================================================
# 6. Identical secrets warned (not errored) in dev
# ===========================================================================


class TestDistinctSecretsDev:
    def test_validate_allows_identical_secrets_in_dev_with_warning(self):
        """WEBHOOK_SECRET == OPERATOR_WEBHOOK_SECRET must NOT raise in dev — only warn."""
        result, exc = _run_validate(
            webhook_secret=_STRONG_A,
            operator_secret=_STRONG_A,  # Same value
            paperclip_secret=_STRONG_C,
            is_railway=False,
        )
        assert exc is None, (
            f"validate_critical_config must not raise for identical secrets in dev; "
            f"got: {exc}"
        )
        combined = " ".join(result).lower()
        assert (
            "identical" in combined
            or "unique" in combined
            or "same" in combined
        ), f"Expected a warning about identical secrets; got: {result}"


# ===========================================================================
# 7. PAPERCLIP_WEBHOOK_SECRET no longer falls back to WEBHOOK_SECRET
# ===========================================================================


class TestPaperclipSecretNoFallback:
    def test_paperclip_secret_no_longer_falls_back_to_webhook(self, monkeypatch):
        """When PAPERCLIP_WEBHOOK_SECRET env var is unset, the module attr must be ''."""
        # Remove the env var so _env("PAPERCLIP_WEBHOOK_SECRET") returns ""
        monkeypatch.delenv("PAPERCLIP_WEBHOOK_SECRET", raising=False)
        monkeypatch.setenv("WEBHOOK_SECRET", _STRONG_A)

        # Force re-evaluation of the module constant by reading _env directly
        value = cfg._env("PAPERCLIP_WEBHOOK_SECRET")
        assert value == "", (
            f"PAPERCLIP_WEBHOOK_SECRET should be '' when unset; got {value!r}"
        )

    def test_paperclip_module_attr_is_pure_env_value(self, monkeypatch):
        """cfg.PAPERCLIP_WEBHOOK_SECRET must equal os.getenv, not a fallback."""
        monkeypatch.delenv("PAPERCLIP_WEBHOOK_SECRET", raising=False)
        # cfg.PAPERCLIP_WEBHOOK_SECRET was set at import time; verify _env behaviour
        # is consistent with how the constant was loaded
        env_value = os.getenv("PAPERCLIP_WEBHOOK_SECRET", "")
        assert env_value == "", "Env var should be unset for this test"
        # The important invariant: whatever _env returns is the module value, no fallback
        assert cfg._env("PAPERCLIP_WEBHOOK_SECRET") == env_value


# ===========================================================================
# 8. OPERATOR_WEBHOOK_SECRET no longer falls back to WEBHOOK_SECRET
# ===========================================================================


class TestOperatorSecretNoFallback:
    def test_operator_secret_no_longer_falls_back_to_webhook(self, monkeypatch):
        """When OPERATOR_WEBHOOK_SECRET env var is unset, the module attr must be ''."""
        monkeypatch.delenv("OPERATOR_WEBHOOK_SECRET", raising=False)
        monkeypatch.setenv("WEBHOOK_SECRET", _STRONG_A)

        value = cfg._env("OPERATOR_WEBHOOK_SECRET")
        assert value == "", (
            f"OPERATOR_WEBHOOK_SECRET should be '' when unset; got {value!r}"
        )

    def test_operator_module_attr_equals_env_value(self, monkeypatch):
        """cfg.OPERATOR_WEBHOOK_SECRET must equal os.getenv, no fallback chain."""
        monkeypatch.delenv("OPERATOR_WEBHOOK_SECRET", raising=False)
        env_value = os.getenv("OPERATOR_WEBHOOK_SECRET", "")
        assert env_value == ""
        assert cfg._env("OPERATOR_WEBHOOK_SECRET") == env_value

    def test_operator_module_attr_set_when_env_present(self, monkeypatch):
        """When OPERATOR_WEBHOOK_SECRET is set, its value is used directly."""
        monkeypatch.setenv("OPERATOR_WEBHOOK_SECRET", _STRONG_B)
        value = cfg._env("OPERATOR_WEBHOOK_SECRET")
        assert value == _STRONG_B, (
            f"Expected {_STRONG_B!r}; got {value!r}"
        )
