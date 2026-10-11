"""Local web server for Orchestra — wraps the SAME `Orchestra.handle()`
pipeline the CLI uses. No business logic here; this is a thin transport
layer so a browser tab can do what `orchestra chat` already does.

Operational behaviour worth knowing:
- At most `max_concurrent_runs` pipelines execute at once; extra requests
  wait for a slot (the stream says so) instead of piling onto Ollama.
- The assistant's reply is saved to history by the server, not the browser,
  so a closed tab does not lose the answer and history stays a valid
  user/assistant alternation even when a run fails.
- The SSE stream sends a keep-alive comment every few seconds so proxies
  and browsers do not drop a connection while a local model is thinking.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..core.config import settings
from ..engine.pipeline import Orchestra, RunReport
from ..llm.health import cached_ollama_status, start_warmup, warm_state
from ..observability import history

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_HEARTBEAT_S = 10.0


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if settings.warm_on_start and settings.llm_backend != "mock":
        start_warmup()          # background thread: never delays startup
    yield


app = FastAPI(title="Orchestra", version=__version__, lifespan=_lifespan)
_orchestra = Orchestra()  # one registry/instance for the process lifetime
_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)


class ChatRequest(BaseModel):
    message: str = Field("", max_length=settings.max_message_chars)
    session_id: str | None = None   # None -> start a new session
    regenerate: bool = False        # replay the last user turn, drop its reply


class TaskView(BaseModel):
    category: str
    specialist: str | None
    status: str
    description: str


class ChatResponse(BaseModel):
    reply: str
    run_id: str
    ok: bool
    tasks: list[TaskView]
    session_id: str


class SessionView(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float


class MessageView(BaseModel):
    id: str
    role: str
    text: str
    run_id: str | None
    created_at: float


class RenameRequest(BaseModel):
    title: str


def _resolve_turn(req: ChatRequest) -> tuple[str, str]:
    """Work out which session this turn belongs to and what text to run.

    Two modes collapse into one contract so /api/chat and /api/chat/stream
    can't drift apart:
      normal      -> (session, req.message), user turn appended
      regenerate  -> (session, last user text), trailing reply dropped
    """
    if req.regenerate:
        if not req.session_id:
            raise HTTPException(400, "regenerate requires a session_id")
        if not history.get_session(req.session_id):
            raise HTTPException(404, "session not found")
        text = history.rewind_to_last_user(req.session_id)
        if not text:
            raise HTTPException(400, "nothing to regenerate in this session")
        return req.session_id, text

    if not req.message.strip():
        raise HTTPException(400, "message must not be empty")
    if req.session_id and not history.get_session(req.session_id):
        raise HTTPException(404, "session not found")
    session_id = req.session_id or history.create_session(req.message).id
    history.add_message(session_id, "user", req.message)
    return session_id, req.message


def _run(text: str, session_id: str, on_event=None) -> RunReport:
    """Run the pipeline in a free slot and persist the reply."""
    if not _slots.acquire(blocking=False):
        if on_event:
            on_event("Waiting for a free slot…")
        _slots.acquire()
    try:
        report = _orchestra.handle(text, on_event)
    finally:
        _slots.release()
    try:
        history.add_message(session_id, "assistant", report.reply,
                            run_id=report.run_id)
    except sqlite3.Error:  # e.g. the chat was deleted while the run was going
        logger.warning("could not save the reply for session %s", session_id)
    return report


def _response(report: RunReport, session_id: str) -> ChatResponse:
    return ChatResponse(
        reply=report.reply, run_id=report.run_id, ok=report.ok,
        session_id=session_id,
        tasks=[TaskView(category=t.category, specialist=t.assigned_to or None,
                        status=t.status.value, description=t.description)
               for t in report.tasks])


@app.get("/api/health")
def health() -> dict:
    """What the browser needs to show an honest status line: which backend
    is really answering, whether it is actually ready, and which models.
    Cheap enough to poll (Ollama is probed at most every few seconds)."""
    llm = cached_ollama_status()
    return {
        "ok": True,                       # this server is up
        "ready": llm["reachable"] and not llm["missing"],
        "version": __version__,
        "backend": settings.llm_backend,
        "main_model": settings.main_model,
        "fast_model": settings.fast_model,
        "concurrency": settings.max_concurrency,
        "specialists": len(_orchestra.registry.specs()),
        "llm": {"reachable": llm["reachable"], "missing": llm["missing"],
                "error": llm["error"]},
        "warm": warm_state(),
    }


@app.get("/api/roster")
def roster() -> list[dict]:
    """The specialist list, for the sidebar — same registry the CLI uses."""
    return [{"name": s.name, "categories": s.categories}
            for s in _orchestra.registry.specs()]


@app.get("/api/sessions", response_model=list[SessionView])
def sessions_list():
    return [SessionView(**s.__dict__) for s in history.list_sessions()]


@app.get("/api/sessions/{session_id}", response_model=list[MessageView])
def session_messages(session_id: str):
    if not history.get_session(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return [
        MessageView(id=m.id, role=m.role, text=m.text,
                    run_id=m.run_id, created_at=m.created_at)
        for m in history.list_messages(session_id)
    ]


@app.patch("/api/sessions/{session_id}", response_model=SessionView)
def session_rename(session_id: str, req: RenameRequest):
    if not history.get_session(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    history.rename_session(session_id, req.title)
    return SessionView(**history.get_session(session_id).__dict__)


@app.delete("/api/sessions/{session_id}")
def session_delete(session_id: str):
    if not history.get_session(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    history.delete_session(session_id)
    return {"ok": True}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session_id, text = _resolve_turn(req)
    return _response(_run(text, session_id), session_id)


def _sse(item: dict) -> str:
    return f"data: {json.dumps(item)}\n\n"


def _next_event(q: queue.Queue):
    """Block for the next event; None means 'nothing yet' (send a heartbeat)."""
    try:
        return q.get(timeout=_HEARTBEAT_S)
    except queue.Empty:
        return None


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Same pipeline as /api/chat, but streams REAL progress as it happens
    (planning, routing, per-specialist completion) instead of one blocking
    wait. Uses Server-Sent Events; the pipeline runs in its own thread so
    the async loop stays free to flush each event as it's emitted."""
    q: queue.Queue = queue.Queue()
    session_id, user_text = _resolve_turn(req)

    # The executor emits human-readable progress lines. Tagging each one
    # with the specialist it names lets the browser light its roster while
    # the run is happening, instead of parsing prose on the client.
    spec_names = [s.name for s in _orchestra.registry.specs()]

    def emit(text: str) -> None:
        named = next((n for n in spec_names if n in text), None)
        q.put({"type": "progress", "text": text, "specialist": named})

    def worker() -> None:
        try:
            report = _run(user_text, session_id, emit)
            q.put({"type": "done", "data": _response(report, session_id).model_dump()})
        except Exception:  # noqa: BLE001 - one bad run must not kill the server
            logger.exception("stream worker crashed")
            q.put({"type": "error",
                   "text": "Something went wrong. The details are in the server log."})

    threading.Thread(target=worker, name="orchestra-run", daemon=True).start()

    async def event_source():
        # session_id first, so the browser can link the new chat to the
        # sidebar even before the first task finishes.
        yield _sse({"type": "session", "id": session_id})
        loop = asyncio.get_running_loop()
        while True:
            item = await loop.run_in_executor(None, _next_event, q)
            if item is None:
                yield ": keep-alive\n\n"
                continue
            yield _sse(item)
            if item["type"] in ("done", "error"):
                break

    return StreamingResponse(
        event_source(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Static frontend last, so /api/* above takes precedence over the catch-all.
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
