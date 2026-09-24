"""Request-local anonymous session identity for artifact ownership."""

from contextvars import ContextVar


session_id: ContextVar[str | None] = ContextVar("session_id", default=None)
