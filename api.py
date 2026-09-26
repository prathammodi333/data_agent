"""Public HTTP boundary for the portfolio data agent."""

import asyncio
from contextvars import copy_context
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from utils.etl_tools import ETLRejected, artifact_info
from utils.database import UnsafeQuery
from utils.anonymous_sessions import AnonymousSessionRegistry
from utils.session_context import session_id as current_session_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
app = FastAPI(title="AI Data Agent", version="1.7")
origins = [value.strip() for value in os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173").split(",") if value.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"], allow_credentials=True)
_pool = ThreadPoolExecutor(max_workers=2)
_slots = asyncio.Semaphore(2)
_recent = defaultdict(deque)
_rate_lock = asyncio.Lock()
_sessions = AnonymousSessionRegistry()
SESSION_COOKIE = "data_agent_session"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    source_artifact_id: uuid.UUID | None = None
    source_mode: Literal["auto", "artifact"] = "auto"


class ErrorInfo(BaseModel):
    code: str
    message: str


class ChatResponse(BaseModel):
    request_id: str
    status: Literal["completed", "blocked", "unsupported", "failed"]
    route: Literal["sql", "etl"] | None = None
    answer: str
    table: dict | None = None
    artifact: dict | None = None
    details: dict
    error: ErrorInfo | None = None


@app.get("/api/health")
def health():
    return {"status": "ok"}


def _set_session_cookie(response: Response, session_id: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=_sessions.ttl_seconds,
        httponly=True,
        samesite="none" if request.url.scheme == "https" else "lax",
        secure=request.url.scheme == "https",
    )


def _session_for_request(request: Request, response: Response, allow_restart: bool = False) -> str:
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        if not _sessions.is_active(session_id):
            if allow_restart:
                session_id, _ = _sessions.create()
                _set_session_cookie(response, session_id, request)
                return session_id
            raise HTTPException(410, detail={"code": "SESSION_EXPIRED", "message": "This demo session has expired. Reload the page to start a new session."})
        return session_id
    session_id, _ = _sessions.create()
    _set_session_cookie(response, session_id, request)
    return session_id


@app.get("/api/session")
def session(request: Request, response: Response):
    session_id = _session_for_request(request, response, allow_restart=True)
    return {"session_id": session_id, "expires_in": _sessions.remaining_seconds(session_id)}


async def _throttle(request: Request):
    # Process-local backstop; configure an edge/provider limit for multi-instance deployments.
    client = request.client.host if request.client else "unknown"
    async with _rate_lock:
        now = time.monotonic()
        recent = _recent[client]
        while recent and now - recent[0] > 3600:
            recent.popleft()
        if len(recent) >= int(os.environ.get("REQUESTS_PER_HOUR", "20")):
            raise HTTPException(429, detail={"code": "RATE_LIMIT", "message": "Demo limit reached. Please try later."})
        recent.append(now)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request, response: Response):
    message = body.message.strip()
    if not message:
        raise HTTPException(422, detail={"code": "EMPTY_MESSAGE", "message": "Enter a question."})
    session_id = _session_for_request(request, response)
    await _throttle(request)
    request_id = str(uuid.uuid4())
    started = time.monotonic()
    try:
        # Refuse surplus work rather than queueing unbounded costly LLM calls.
        await asyncio.wait_for(_slots.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        raise HTTPException(429, detail={"code": "BUSY", "message": "The demo is busy. Please retry shortly."}) from None
    try:
        loop = asyncio.get_running_loop()
        from services.agent_adapter import invoke_agent
        source_id = str(body.source_artifact_id) if body.source_artifact_id else None
        token = current_session_id.set(session_id)
        try:
            context = copy_context()
        finally:
            current_session_id.reset(token)
        result = await asyncio.wait_for(
            loop.run_in_executor(_pool, context.run, invoke_agent, message, request_id, source_id, body.source_mode),
            timeout=45,
        )
        logger.info("request=%s route=%s status=%s elapsed=%.1f", request_id, result["route"], result["status"], time.monotonic() - started)
        return ChatResponse(request_id=request_id, **result)
    except asyncio.TimeoutError:
        logger.warning("request=%s timeout elapsed=%.1f", request_id, time.monotonic() - started)
        raise HTTPException(504, detail={"code": "TIMEOUT", "message": "The request took too long. Try again."}) from None
    except (UnsafeQuery, ETLRejected) as exc:
        logger.warning("request=%s blocked reason=%s", request_id, exc)
        return ChatResponse(request_id=request_id, status="blocked", route=None,
                            answer="This request could not be completed.", table=None, artifact=None,
                            details={"sql": None, "sql_safe": False, "operation": None},
                            error=ErrorInfo(code="BLOCKED", message=str(exc)))
    except Exception as exc:
        logger.exception("request=%s agent_failed", request_id)
        raise HTTPException(503, detail={"code": "UNAVAILABLE", "message": "The demo is temporarily unavailable."}) from None
    finally:
        _slots.release()


@app.get("/api/artifacts/{artifact_id}")
def download(artifact_id: str, request: Request, response: Response):
    session_id = _session_for_request(request, response)
    token = current_session_id.set(session_id)
    try:
        path, metadata = artifact_info(artifact_id)
    except (FileNotFoundError, ETLRejected, ValueError):
        raise HTTPException(404, detail={"code": "ARTIFACT_MISSING", "message": "This download has expired."}) from None
    finally:
        current_session_id.reset(token)
    media = {"csv": "text/csv", "json": "application/x-ndjson", "parquet": "application/octet-stream"}
    return FileResponse(path, media_type=media[metadata["format"]], filename=metadata["name"])
