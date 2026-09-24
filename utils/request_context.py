"""Correlate agent logs with the API request without logging the user's prompt."""

from contextvars import ContextVar


request_id: ContextVar[str] = ContextVar("request_id", default="local")
