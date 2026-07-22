# Orchestra v2 — Local Multi-Agent Orchestrator

Production-grade rebuild of the v1 proof-of-concept. Zero cost, fully local,
built for a Linux Mint machine running Ollama with qwen2.5:7b.

## Architecture (the company)

```
user message
    │
    ▼
 Planner (main model, hardened JSON)  ── reads the specialist catalog,
    │                                    outputs typed Tasks + dependencies
    ▼
 Executor (dependency waves, concurrency=2, retries)
    │        └─ deterministic category→specialist lookup (no LLM routing)
    │        └─ one shared ReAct engine runs every specialist
    ▼
 Aggregator (fast model) ── one coherent reply; honest about failures
```

Key modules:

| Path | Role |
|---|---|
| `orchestra/core/config.py` | all settings, env-overridable (`ORCHESTRA_*`) |
| `orchestra/core/contracts.py` | Task / SpecialistSpec / state types |
| `orchestra/llm/adapter.py` | backend-agnostic LLM interface, JSON repair, retries |
| `orchestra/llm/backends.py` | OllamaClient + MockLLM + factory |
| `orchestra/agents/toolbox.py` | tool registry (name-safe by design) |
| `orchestra/agents/factory.py` | SpecialistRegistry + the one ReAct engine |
| `orchestra/engine/` | planner / executor / aggregator / pipeline |
| `orchestra/observability/telemetry.py` | SQLite spans, `stats` report |

## Setup (target machine)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ollama pull qwen2.5:7b            # main tier (you already have it)
ollama pull qwen2.5:3b            # OPTIONAL fast tier — recommended
python -m orchestra doctor        # pre-flight check
```

If you pulled the 3b model, enable the fast tier:

```bash
export ORCHESTRA_FAST_MODEL=qwen2.5:3b
```

## Run

```bash
python -m orchestra chat          # interactive (/audit /stats /quit)
python -m orchestra ask "What is 6*7?" --audit
python smoke_test.py              # benchmark — send Claude the output
python -m orchestra stats         # real numbers from telemetry
```

## Tests (run anywhere, no Ollama needed)

```bash
pip install pytest pydantic pydantic-settings
python -m pytest tests/           # 35 tests, all logic mocked
```

## Adding a specialist (5 lines, zero risk)

```python
registry.register(SpecialistSpec(
    name="Translator",
    categories=["translation"],
    system_prompt="Translate the given text faithfully.",
))
```

## Adding a tool

```python
from orchestra.agents.toolbox import tool

@tool
def reverse_text(text: str) -> str:
    """Reverse the characters of `text`."""   # docstring = LLM manual
    return text[::-1]
```

## Honest limits

- qwen2.5:7b will not match frontier models on deep reasoning or fine prose.
  The architecture compensates on STRUCTURED work (routing, tools, memory).
- Concurrency overlaps I/O; Ollama mostly serializes inference on one GPU/CPU.
- Compound requests take roughly (planner + tasks + aggregator) model calls —
  expect tens of seconds, not instant. `stats` shows the real numbers.
