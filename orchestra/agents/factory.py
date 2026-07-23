"""Agent Factory — one shared ReAct engine + a registry of job descriptions.

The professional insight: a "specialist" is NOT code. It is DATA — a
SpecialistSpec (name, categories, prompt, tools, tier). One engine executes
any spec. Hiring a new employee = registering 5 lines of data, zero new
logic, zero risk to existing agents.
"""
from __future__ import annotations

import logging

from ..core.contracts import SpecialistSpec, Task, TaskStatus
from ..llm.adapter import LLMClient, LLMError
from ..observability.telemetry import Telemetry
from .toolbox import get_tools, run_tool

logger = logging.getLogger(__name__)

# Max chars of a tool result re-injected into the ReAct transcript. Keeps
# each step cheap on a local 7B model regardless of how big a page/file is.
_TOOL_CTX_CAP = 2000


# ── Registry ───────────────────────────────────────────────────────
class SpecialistRegistry:
    def __init__(self):
        self._specs: dict[str, SpecialistSpec] = {}

    def register(self, spec: SpecialistSpec) -> "SpecialistRegistry":
        if spec.name in self._specs:
            raise ValueError(f"specialist '{spec.name}' registered twice")
        get_tools(spec.tool_names)  # validate tool names at HIRE time
        self._specs[spec.name] = spec
        logger.info("REGISTRY | hired %s (categories=%s, tools=%s)",
                    spec.name, spec.categories, spec.tool_names)
        return self

    def find_for(self, category: str) -> SpecialistSpec | None:
        for spec in self._specs.values():
            if category in spec.categories:
                return spec
        return None

    def catalog(self) -> str:
        """Human/LLM-readable list of who works here — used by the planner
        so it only creates tasks the company can actually do."""
        return "\n".join(
            f"- {s.name}: handles [{', '.join(s.categories)}]"
            for s in self._specs.values()
        )

    def hints(self) -> str:
        """Category meanings, built from each spec's own hint. New hires
        teach the planner about themselves automatically."""
        lines = []
        for s in self._specs.values():
            if s.hint:
                for c in s.categories:
                    lines.append(f"- {c}: {s.hint}")
        return "\n".join(lines)

    @property
    def all_categories(self) -> list[str]:
        cats: list[str] = []
        for s in self._specs.values():
            cats.extend(c for c in s.categories if c not in cats)
        return cats


# ── The one ReAct engine ───────────────────────────────────────────
def run_specialist(
    spec: SpecialistSpec,
    task: Task,
    llm: LLMClient,
    tel: Telemetry,
) -> Task:
    """Execute one task with one specialist. Always returns the task with a
    final status — DONE with a result, or FAILED with a reason. Never raises
    past this boundary: one employee's crash must not kill the company.
    """
    tools = get_tools(spec.tool_names)
    messages = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": task.description},
    ]
    task = task.model_copy(update={
        "status": TaskStatus.RUNNING,
        "assigned_to": spec.name,
        "attempts": task.attempts + 1,
    })

    try:
        with tel.span("specialist", role=spec.name, task=task.description[:80]):
            for _ in range(spec.max_steps):
                reply = llm.chat(messages, tools=tools or None,
                                 temperature=0.2)
                if not reply.wants_tools:
                    return task.model_copy(update={
                        "status": TaskStatus.DONE,
                        "result": reply.text.strip() or "(empty reply)",
                    })
                # transcribe the assistant turn, then execute each call
                messages.append({"role": "assistant",
                                 "content": reply.text or "[tool calls]"})
                for tc in reply.tool_calls:
                    output = run_tool(tc.name, tc.args)
                    # Cap what goes BACK into the transcript. A huge tool
                    # output (a web page, a big file) otherwise gets re-sent
                    # to the model on every subsequent step, making each step
                    # slower than the last. The full output still reaches the
                    # user via a terminal tool's result or the aggregator.
                    ctx = output if len(output) <= _TOOL_CTX_CAP else (
                        output[:_TOOL_CTX_CAP]
                        + f"\n...[truncated {len(output) - _TOOL_CTX_CAP} chars]")
                    messages.append({"role": "tool", "content": ctx,
                                     "tool_call_id": tc.id})
                    # Fatal-on-error tools: stop HERE, verbatim, before the
                    # model gets another turn to paper over the failure.
                    if tc.name in spec.fatal_tools and output.startswith("Error"):
                        return task.model_copy(update={
                            "status": TaskStatus.FAILED,
                            "result": output,
                        })
                    # Terminal tool ran cleanly -> task is DONE now. This
                    # breaks the "call, re-call, re-call" loop that small
                    # models fall into on save-style tasks.
                    if (tc.name in spec.terminal_tools
                            and not output.startswith("Error")):
                        return task.model_copy(update={
                            "status": TaskStatus.DONE,
                            "result": output,
                        })
            return task.model_copy(update={
                "status": TaskStatus.FAILED,
                "result": f"step limit ({spec.max_steps}) reached",
            })
    except LLMError as exc:
        logger.error("SPECIALIST %s | LLM gave up: %s", spec.name, exc)
        return task.model_copy(update={
            "status": TaskStatus.FAILED,
            "result": f"LLM unavailable: {exc}",
        })


# ── Default staff (mirrors v1 capabilities, hired the v2 way) ──────
def default_registry() -> SpecialistRegistry:
    from . import builtin_tools, file_tools, web_tools  # noqa: F401  (registers tools)

    reg = SpecialistRegistry()
    reg.register(SpecialistSpec(
        name="Memory Keeper",
        hint="store a NEW fact the user states about themselves.",
        categories=["memory_save"],
        system_prompt=(
            "You save facts about the user. Call remember_about_user exactly "
            "once with a short `key` and the EXACT `fact` from the task."),
        tool_names=["remember_about_user"],
        terminal_tools=["remember_about_user"],
        llm_tier="fast",
        max_steps=4,
    ))
    reg.register(SpecialistSpec(
        name="Memory Lookup",
        hint="retrieve a fact the user asks about themselves, e.g. 'what is my favorite X', 'do you remember my Y'. ALWAYS memory_recall, never general.",
        categories=["memory_recall"],
        system_prompt=(
            "You retrieve stored user facts. Call recall_about_user, then "
            "answer the question using ONLY what it returns."),
        tool_names=["recall_about_user"],
        llm_tier="fast",
        max_steps=3,
    ))
    reg.register(SpecialistSpec(
        name="Math Solver",
        hint="arithmetic on numbers - add, multiply, divide.",
        categories=["math"],
        system_prompt=(
            "You solve arithmetic using tools — never mental math. Extract "
            "the numbers, call the right tool(s), report the final answer."),
        tool_names=["add", "multiply", "divide"],
        max_steps=6,
    ))
    reg.register(SpecialistSpec(
        name="Writer",
        hint="produce creative or drafted text (haiku, email, story, report).",
        categories=["writing"],
        system_prompt=(
            "You are a skilled writer. Produce the requested piece "
            "beautifully and concisely. Write ONLY in English unless the "
            "task explicitly asks for another language. Never mix languages. "
            "No preamble."),
        max_steps=2,
    ))
    reg.register(SpecialistSpec(
        name="Text Analyst",
        hint="anything about the CHARACTERS or WORDS of a string - counting letters or words. 'How many times does X appear' is ALWAYS text_analysis, never math.",
        categories=["text_analysis"],
        system_prompt=(
            "You analyze text with tools. Extract the exact text from the "
            "task, call the right counting tool, report the number clearly."),
        tool_names=["count_letters", "word_count"],
        llm_tier="fast",
        max_steps=4,
    ))
    reg.register(SpecialistSpec(
        name="Time Keeper",
        hint="the current date or time.",
        categories=["time"],
        system_prompt="Report the current time using get_current_time.",
        tool_names=["get_current_time"],
        llm_tier="fast",
        max_steps=3,
    ))
    reg.register(SpecialistSpec(
        name="File Clerk",
        categories=["files"],
        hint=("create, read, append, or list files in the agent workspace - "
              "'save this to a file', 'read notes.txt', 'what files are there'."),
        system_prompt=(
            "You manage files in the agent workspace using your tools. "
            "Use relative paths only (e.g. 'notes.txt'). When asked to save "
            "content, call write_file ONCE with the exact content (if the "
            "task includes 'Context from earlier steps', that context IS the "
            "content to summarize/save). When asked to read, call read_file "
            "ONCE with the exact filename."),
        tool_names=["write_file", "read_file", "append_file", "list_files"],
        terminal_tools=["write_file", "read_file", "append_file", "list_files"],
        max_steps=4,
    ))
    reg.register(SpecialistSpec(
        name="Web Reader",
        categories=["web"],
        hint=("fetch and read a SPECIFIC web page the user gives a URL for - "
              "'summarize this page', 'what does this article say'. Requires "
              "a URL; it is not a search engine."),
        system_prompt=(
            "You read web pages. Call fetch_webpage with the exact URL from "
            "the task, then answer the question using ONLY the fetched text. "
            "If the fetch fails, report the error honestly."),
        tool_names=["fetch_webpage"],
        max_steps=4,
    ))
    reg.register(SpecialistSpec(
        name="General Assistant",
        categories=["general"],
        system_prompt=(
            "You are a helpful, concise assistant. Answer directly. If the "
            "question is about the user, call recall_about_user first."),
        tool_names=["recall_about_user"],
        max_steps=4,
    ))
    return reg
