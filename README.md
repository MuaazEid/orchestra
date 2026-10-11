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

- **Fails fast and says why** — Ollama not running or a model not installed raises one actionable error (`ollama serve` / `ollama pull <model>`) instead of a retry storm; a real request timeout is enforced; permanent failures are never retried.
- **Planner guardrails** — deterministic checks around the planner's output: plain questions skip the planner entirely, tool tasks the message gives no evidence for are demoted, plans are deduplicated, capped and cycle-free.
- **Network-safe web tool** — `fetch_webpage` refuses loopback, private and link-local addresses (including via redirects), because the URL comes from a model and the page can carry prompt injection.
- **Operable web server** — honest `/api/health` (Ollama reachable? models installed? warm?), background model warm-up, SSE keep-alives, bounded concurrent runs, replies saved server-side.

**199 unit tests**, all runnable without Ollama via a scripted `MockLLM` — the whole graph, including the web transport layer, is testable on any machine. CI runs them on Python 3.11 and 3.13, plus a lint pass.

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

## Configuration

Everything is an environment variable with the `ORCHESTRA_` prefix (or a line in `.env`). The ones worth knowing:

| Variable | Default | What it does |
|---|---|---|
| `ORCHESTRA_MAIN_MODEL` / `ORCHESTRA_FAST_MODEL` | `qwen2.5:7b` | Ollama models for planning/work and for quick tasks |
| `ORCHESTRA_NUM_CTX` | `8192` | Context window requested from Ollama. Ollama's own default is 4096 and it truncates silently beyond that |
| `ORCHESTRA_KEEP_ALIVE` | `30m` | How long Ollama keeps a model loaded between requests |
| `ORCHESTRA_REQUEST_TIMEOUT_S` | `120` | Per-request timeout to Ollama |
| `ORCHESTRA_WARM_ON_START` | `true` | Load the models in the background when the web server starts |
| `ORCHESTRA_MAX_CONCURRENT_RUNS` | `2` | Pipelines the web server runs at once; extra requests wait |
| `ORCHESTRA_MAX_MESSAGE_CHARS` | `8000` | Longest accepted chat message |
| `ORCHESTRA_PLANNER_FAST_PATH` | `true` | Skip the planner for messages that show no sign of needing a specialist |
| `ORCHESTRA_ALLOW_PRIVATE_FETCH` | `false` | Let `fetch_webpage` reach private/loopback addresses (off for safety) |
| `ORCHESTRA_TAVILY_API_KEY` | – | Optional web-search key; keep it in `.env`, never in code |

`python -m orchestra doctor` checks Ollama, the models and these settings before first run.

## Web UI

`python -m orchestra serve` starts a local FastAPI server (same `Orchestra.handle()`
the CLI uses — no separate logic) and opens a browser-based chat at
`localhost:8765`.

The roster on the left lights up and connects **while the run is happening** —
each progress event is tagged server-side with the specialist it names, so the
constellation is a live trace of the same planner → executor → aggregator flow
`/audit` prints as text in the CLI. Every reply keeps its audit panel: task
status, category, and the specialist that was resolved for it.

| | |
|---|---|
| **Persistent history** | SQLite-backed sessions in a sidebar — rename inline, delete with a confirm step, resume after a restart |
| **Real Markdown** | headings, lists, tables, blockquotes, and code blocks with per-block copy |
| **Regenerate** | replays the last user turn against a fresh run; history is rewound first, so the transcript is never left with two answers to one question |
| **Command palette** | `Ctrl/⌘ K` — fuzzy search across chats and commands |
| **Keyboard-first** | `N` new · `/` focus · `R` regenerate · `C` copy · `T` theme · `B` sidebar · `[` `]` chats · `?` shortcuts |
| **Light and dark** | follows the OS by default, applied before first paint so it never flashes |

**No CDN, no build step, no `node_modules`.** The client is three static files
served straight off disk; even the Markdown renderer is vendored (~200 lines,
escape-first so model output can never become live HTML). Orchestra's premise is
that it runs with no cloud — a UI that blocked first paint on a Google font
would quietly break that on the offline machine it was built for. There's a test
that fails the build if an external URL ever creeps back in.

## Project layout

| Path | Role |
|---|---|
| `orchestra/core/` | config (env-driven, validated at boot) · typed contracts |
| `orchestra/llm/` | backend-agnostic adapter · Ollama client · MockLLM · readiness + warm-up |
| `orchestra/agents/` | tool registry · specialist factory · shared ReAct engine |
| `orchestra/engine/` | planner · planner guardrails · dependency executor · aggregator · pipeline |
| `orchestra/observability/` | SQLite telemetry + stats reports |
| `orchestra/web/` | FastAPI transport + a dependency-free browser client |
| `tests/` | 199 tests covering routing, loops, sandboxing, planner guardrails, failure paths, SSRF, history, HTTP, language pinning |

## Honest limits

A 7B local model will not match frontier models on open-ended reasoning or fine prose. The qwen family also drifts into Chinese under pressure; Orchestra pins the reply language per-request and retries once if CJK characters leak, falling back to a labeled join rather than returning the wrong language. This architecture compensates on **structured** work — routing, tool use, memory, file operations — where measured task success is 100% across the benchmark suite. Concurrency overlaps I/O; Ollama largely serializes inference on a single GPU. Complex chains take minutes, not seconds. All of this is by design for the zero-cost constraint.

Two more limits to know about. The planner fast path and demotion rules rely on English and Arabic keyword signals: a message in another language with no digits or URLs is answered by the general assistant and will not trigger a specialist (set `ORCHESTRA_PLANNER_FAST_PATH=false` and the demotion still applies only to the categories it knows). And `fetch_webpage` resolves a hostname once for its safety check and again when it connects, so a hostile DNS server could still rebind between the two; closing that fully means connecting to the validated IP.

## Author

**Muaaz Eidalla** — Senior AI Engineer, Riyadh. Generative AI · multi-agent orchestration · local LLM deployment · computer vision.

MIT License
