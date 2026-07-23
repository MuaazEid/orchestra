# Orchestra — Local Multi-Agent Orchestrator

A production-grade multi-agent AI system that runs **entirely on local hardware** at **zero cost** — no cloud APIs, no subscriptions. Built with Python and Ollama (qwen2.5:7b) on a 16GB consumer laptop.

> A planner decomposes user requests into typed tasks, a dependency-aware executor routes them to specialist agents running a shared ReAct engine, and an aggregator weaves the results into one coherent reply — with telemetry, retries, and graceful degradation at every layer.

```
user message
    │
    ▼
 Planner (hardened JSON output + deterministic routing backstop)
    │        reads the specialist catalog → typed Tasks + dependencies
    ▼
 Executor (dependency waves · bounded concurrency · per-task retries)
    │        deterministic category→specialist lookup — no LLM routing
    │        one shared ReAct engine runs every specialist
    ▼
 Aggregator (honest about partial failures)
```

## Why Orchestra? (not just another LangGraph/CrewAI wrapper)

LangGraph, CrewAI, and similar frameworks are built assuming a strong model underneath — GPT-4-class reasoning that can recover from ambiguity on its own. **Orchestra is built for the opposite assumption: a small model (7B) running on a laptop with no GPU budget to spare, and no cloud fallback.** Every design choice here exists to make *that specific model class* behave reliably:

- Routing is a deterministic lookup, not an LLM guess — because a 7B model's category judgment is exactly the kind of "50-50 coin flip" a bigger model wouldn't need help with.
- A successful tool call can end a task immediately (**terminal tools**) — because small models narrate after acting, and that narration is where they loop.
- Tool context is capped and dependency injection is trimmed — because a 7B model's context window turns into its own worst enemy the moment a tool result is more than a paragraph long.

If you have GPT-4o or Claude behind your agents, you may not need any of this — a strong model papers over these failure modes on its own. Orchestra exists for the zero-cost, fully-local case, where those failure modes are the whole engineering problem.

## Hardened through failure, not theory

Most agent demos work once, on the happy path. This system was hardened through **10 rounds of live debugging against a real local model**, each round exposing a different failure class. The full story is in the design notes below — because diagnosing failures is the actual job of an AI engineer.

| Round | Failure discovered | Fix class |
|---|---|---|
| 1 | Planner misrouted tasks (counting → math) | Few-shot prompt + **deterministic keyword backstop** |
| 2 | Small model looped on save-tasks (77s wasted) | **Terminal tools** — a successful side-effect completes the task, breaking loops by design |
| 3 | Recall filter required exact phrase match | Word-level matching + fallback-to-all |
| 4 | Model-tiering experiment made everything *slower* | Measured, found GPU memory swapping — reverted. **Numbers over theory** |
| 5–7 | Long web pages ballooned the ReAct transcript (13-min tasks) | Context caps at the tool boundary + trimmed dependency injection |
| 8–10 | Read-tasks poisoned by injected context | Surgical context rules + full tool-call observability |

Final result: an 8-step web→summarize→save→read chain that initially took **800s and failed** now completes in **139s** — verified by built-in telemetry, not vibes.

## End-to-end example

Input:
```
My name is Muaaz. Multiply 6 by 7, and write a one-line haiku about the moon.
```

**1. Planner** reads the specialist catalog and emits typed tasks (category, not agent name — the executor resolves that deterministically):
```json
{"tasks": [
  {"id": "t1", "category": "math",    "description": "Multiply 6 by 7"},
  {"id": "t2", "category": "writing", "description": "Write a one-line haiku about the moon"}
]}
```

**2. Executor** finds no dependency between t1/t2, runs both in the same wave, resolves `category → specialist` via the registry (`math → Math Solver`, `writing → Writer`), and each specialist runs the shared ReAct loop against its own tools.

**3. Aggregator** composes the two results into one reply:
```
Muaaz, the product of 6 multiplied by 7 is 42. Here's a haiku about the moon:
Silver glow in night's embrace.
```

**4. Telemetry** (`orchestra stats`), from an actual run on the reference laptop (Ollama, qwen2.5:7b):
```
backend=ollama main=qwen2.5:7b fast=qwen2.5:7b concurrency=2
[PASS] simple math   (16.3s, 1 task)
[PASS] memory save   (8.0s,  1 task)
[PASS] memory recall (8.8s,  1 task)
[PASS] counting      (10.6s, 1 task)
[PASS] compound       (25.8s, 3 tasks)   ← the example above
=== 5/5 passed ===

node          runs   ok%   avg_s   max_s
specialist      46   100%   6.94   22.59
run             32   100%  17.93   77.58
planner         32   100%   7.68   18.65
executor        32   100%   9.07   67.23
aggregator       7   100%   5.31    8.03
```

## Engineering highlights

- **Hardened structured output** — small local models emit broken JSON; the adapter extracts, repairs, and retries with self-correction feedback before ever failing.
- **Failure isolation** — one agent crashing (or Ollama itself dying mid-run; observed and survived) never kills the run. Tasks blocked by failed prerequisites fail fast with clear reasons.
- **Specialists are data, not code** — hiring a new agent is ~7 lines registering a `SpecialistSpec` (name, categories, prompt, tools, routing hint). The planner learns new categories automatically from the hint.
- **Tool safety by construction** — registry keys derive from function names (mismatch impossible); arguments are validated against signatures before execution; error messages include the correct signature so the model can self-correct.
- **Sandboxed side effects** — file tools are confined to a workspace with cross-platform path-traversal protection (the `..\` Windows case is unit-tested).
- **Observability first** — every span (planner, specialist, tool call) is timed and logged to SQLite; `orchestra stats` reports real success rates and latencies.

**53 unit tests**, all runnable without Ollama via a scripted `MockLLM` — the whole graph is testable on any machine.

## Specialists at a glance

Hiring a specialist is registering a `SpecialistSpec` — the memory layer below is not a separate subsystem, it's two specialists like any other:

| Specialist | Category | Tools | Notes |
|---|---|---|---|
| Math Solver | `math` | add, multiply, divide | |
| Writer | `writing` | — | pure generation |
| Text Analyst | `text_analysis` | count_letters, word_count | |
| Time Keeper | `time` | get_current_time | |
| **Memory Keeper** | `memory_save` | remember_about_user | SQLite-backed; terminal on save |
| **Memory Lookup** | `memory_recall` | recall_about_user | word-level match + fallback-to-all |
| File Clerk | `files` | read/write/append/list_file | sandboxed workspace; all ops terminal |
| Web Reader | `web` | fetch_webpage | capped excerpt, not a search engine |
| General Assistant | `general` | recall_about_user | fallback when nothing else fits |

The planner never picks a specialist by name — it emits a `category` string, and the executor resolves `category → specialist` through the registry (`SpecialistRegistry.find_for`). That indirection is what makes routing deterministic and testable instead of another LLM guess.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
ollama pull qwen2.5:7b
python -m orchestra doctor        # pre-flight check with actionable fixes
python -m orchestra chat          # interactive CLI (/audit shows the task trail)
python -m orchestra serve         # browser UI at http://localhost:8765
python smoke_test.py              # 6-case benchmark with timings
```

## Web UI

`python -m orchestra serve` starts a local FastAPI server (same `Orchestra.handle()`
the CLI uses — no separate logic) and opens a browser-based chat at
`localhost:8765`. The sidebar roster lights up and connects, in order, as a
request's tasks are routed to specialists — a visual trace of the same
planner → executor → aggregator flow `/audit` shows as text in the CLI.

## Project layout

| Path | Role |
|---|---|
| `orchestra/core/` | config (env-driven, validated at boot) · typed contracts |
| `orchestra/llm/` | backend-agnostic adapter · Ollama client · MockLLM |
| `orchestra/agents/` | tool registry · specialist factory · shared ReAct engine |
| `orchestra/engine/` | planner · dependency executor · aggregator · pipeline |
| `orchestra/observability/` | SQLite telemetry + stats reports |
| `tests/` | 53 tests covering routing, loops, sandboxing, context rules |

## Honest limits

A 7B local model will not match frontier models on open-ended reasoning or fine prose. This architecture compensates on **structured** work — routing, tool use, memory, file operations — where measured task success is 100% across the benchmark suite. Concurrency overlaps I/O; Ollama largely serializes inference on a single GPU. Complex chains take minutes, not seconds. All of this is by design for the zero-cost constraint.

## Author

**Muaaz Eidalla** — Senior AI Engineer, Riyadh. Generative AI · multi-agent orchestration · local LLM deployment · computer vision.

MIT License
