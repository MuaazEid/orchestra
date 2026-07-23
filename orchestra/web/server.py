"""Local web server for Orchestra — wraps the SAME `Orchestra.handle()`
pipeline the CLI uses. No new business logic here; this is a thin transport
layer so a browser tab can do what `orchestra chat` already does.
"""
from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agents.factory import default_registry
from ..core.contracts import TaskStatus
from ..engine.aggregator import aggregate
from ..engine.executor import execute_all
from ..engine.pipeline import Orchestra
from ..engine.planner import plan
from ..llm.backends import get_llm
from ..observability.telemetry import Telemetry

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Orchestra")
_orchestra = Orchestra()  # one registry/instance for the process lifetime


class ChatRequest(BaseModel):
    message: str


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


@app.get("/api/roster")
def roster() -> list[dict]:
    """The specialist list, for the sidebar — same registry the CLI uses."""
    reg = default_registry()
    return [
        {"name": s.name, "categories": s.categories}
        for s in reg._specs.values()  # noqa: SLF001 (read-only introspection)
    ]


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    report = _orchestra.handle(req.message)
    return ChatResponse(
        reply=report.reply,
        run_id=report.run_id,
        ok=report.ok,
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

    def emit(text: str) -> None:
        q.put({"type": "progress", "text": text})

    def run_pipeline() -> None:
        try:
            tel = Telemetry.new_run()
            with tel.span("run", input=req.message[:80]):
                emit("Planning your request\u2026")
                tasks = plan(req.message, _orchestra.registry, get_llm("main"), tel)
                cats = ", ".join(t.category for t in tasks) or "nothing to do"
                emit(f"Planned {len(tasks)} task(s): {cats}")
                tasks = execute_all(tasks, _orchestra.registry, tel, on_event=emit)
                emit("Composing the final reply\u2026")
                reply = aggregate(req.message, tasks, get_llm("fast"), tel)
            data = ChatResponse(
                reply=reply, run_id=tel.run_id,
                ok=all(t.status == TaskStatus.DONE for t in tasks),
                tasks=[TaskView(category=t.category, specialist=t.assigned_to,
                                status=t.status.value, description=t.description)
                       for t in tasks],
            )
            q.put({"type": "done", "data": data.model_dump()})
        except Exception as exc:  # one bad run must not kill the server
            q.put({"type": "error", "text": str(exc)})

    async def event_source():
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
