# Pre-import fastapi to prevent namespace package conflicts during test collection.
import fastapi  # noqa: F401
import fastapi.middleware.trustedhost  # noqa: F401
