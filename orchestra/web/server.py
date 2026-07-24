"""Local web server for Orchestra — wraps the SAME `Orchestra.handle()`
pipeline the CLI uses. No new business logic here; this is a thin transport
layer so a browser tab can do what `orchestra chat` already does.
"""
from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agents.factory import default_registry
from ..core.config import settings
from ..core.contracts import TaskStatus
from ..engine.aggregator import aggregate
from ..engine.executor import execute_all
from ..engine.pipeline import Orchestra
from ..engine.planner import plan
from ..llm.backends import get_llm
from ..observability import history
from ..observability.telemetry import Telemetry

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Orchestra")
_orchestra = Orchestra()  # one registry/instance for the process lifetime


class ChatRequest(BaseModel):
    message: str = ""
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
    session_id = req.session_id or history.create_session(req.message).id
    history.add_message(session_id, "user", req.message)
    return session_id, req.message


@app.get("/api/health")
def health() -> dict:
    """What the browser needs to show an honest status line: which backend
    is really answering, and which models. Cheap enough to poll."""
    reg = default_registry()
    return {
        "ok": True,
        "backend": settings.llm_backend,
        "main_model": settings.main_model,
        "fast_model": settings.fast_model,
        "concurrency": settings.max_concurrency,
        "specialists": len(reg._specs),  # noqa: SLF001 (read-only introspection)
    }


@app.get("/api/roster")
def roster() -> list[dict]:
    """The specialist list, for the sidebar — same registry the CLI uses."""
    reg = default_registry()
    return [
        {"name": s.name, "categories": s.categories}
        for s in reg._specs.values()  # noqa: SLF001 (read-only introspection)
    ]


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
    report = _orchestra.handle(text)
    history.add_message(session_id, "assistant", report.reply, run_id=report.run_id)
    return ChatResponse(
        reply=report.reply,
        run_id=report.run_id,
        ok=report.ok,
        session_id=session_id,
        tasks=[
            TaskView(category=t.category, specialist=t.assigned_to,
                     status=t.status.value, description=t.description)
            for t in report.tasks
        ],
    )


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Same pipeline as /api/chat, but streams REAL progress as it happens
    (planning, routing, per-specialist completion) instead of one blocking
    wait. Uses Server-Sent Events; the pipeline runs in a worker thread so
    the async loop stays free to flush each event as it's emitted."""
    q: queue.Queue = queue.Queue()
    session_id, user_text = _resolve_turn(req)

    # The executor emits human-readable progress lines. Tagging each one
    # with the specialist it names lets the browser light its roster while
    # the run is happening, instead of parsing prose on the client.
    spec_names = [s.name for s in _orchestra.registry._specs.values()]  # noqa: SLF001

    def emit(text: str) -> None:
        named = next((n for n in spec_names if n in text), None)
        q.put({"type": "progress", "text": text, "specialist": named})

    def run_pipeline() -> None:
        try:
            tel = Telemetry.new_run()
            with tel.span("run", input=user_text[:80]):
                emit("Planning your request\u2026")
                tasks = plan(user_text, _orchestra.registry, get_llm("main"), tel)
                cats = ", ".join(t.category for t in tasks) or "nothing to do"
                emit(f"Planned {len(tasks)} task(s): {cats}")
                tasks = execute_all(tasks, _orchestra.registry, tel, on_event=emit)
                emit("Composing the final reply\u2026")
                reply = aggregate(user_text, tasks, get_llm("fast"), tel)
            history.add_message(session_id, "assistant", reply, run_id=tel.run_id)
            data = ChatResponse(
                reply=reply, run_id=tel.run_id,
                ok=all(t.status == TaskStatus.DONE for t in tasks),
                session_id=session_id,
                tasks=[TaskView(category=t.category, specialist=t.assigned_to,
                                status=t.status.value, description=t.description)
                       for t in tasks],
            )
            q.put({"type": "done", "data": data.model_dump()})
        except Exception as exc:  # one bad run must not kill the server
            q.put({"type": "error", "text": str(exc)})

    async def event_source():
        # Emit session_id up front so the browser can link the new chat to
        # the sidebar even before the first task finishes.
        yield f"data: {json.dumps({'type': 'session', 'id': session_id})}\n\n"
        loop = asyncio.get_event_loop()
        pipeline_future = loop.run_in_executor(None, run_pipeline)
        while True:
            item = await loop.run_in_executor(None, q.get)
            yield f"data: {json.dumps(item)}\n\n"
            if item["type"] in ("done", "error"):
                break
        await pipeline_future

    return StreamingResponse(event_source(), media_type="text/event-stream")


# Static frontend last, so /api/* above takes precedence over the catch-all.
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
