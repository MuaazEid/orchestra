# Changelog

## 0.5.0 — stability release

### Fixed
- **Request timeout was never applied.** `ChatOllama` has no `timeout` field and silently ignores unknown arguments, so a stuck Ollama request could hang forever. The timeout now goes through `client_kwargs` (a test builds the real class to guard against regressions).
- **Silent context truncation.** Ollama defaults to a 4096-token window; Orchestra now requests an explicit `num_ctx` (default 8192) and `keep_alive` (30m).
- **Retry storms when Ollama is down.** Connection-refused and model-not-installed are permanent: they raise one actionable error immediately instead of backing off through every retry at every layer.
- **Permanent failures were retried.** Fatal-tool errors and backend-down failures are marked `fatal` and never retried by the executor.
- **One crashing task could abort a whole run.** Every task is now guarded; `Orchestra.handle()` never raises and returns a `RunReport` with `error` set.
- **SQLite connections were never closed** (`with sqlite3.connect()` commits but does not close). One shared helper now closes, commits/rolls back, enables WAL and a busy timeout.
- **Planner invented tool tasks for plain questions.** New deterministic guardrails: fast path, demotion of unjustified categories, dedupe, size cap, cycle breaking.
- **Planner dependency misalignment.** A skipped invalid planner entry used to shift every later `needs` index onto the wrong task.
- **`fetch_webpage` could reach internal addresses (SSRF)**, including through redirects; it now refuses non-public targets. It also decodes pages with their declared charset.
- **Arabic recall queries matched nothing** (the word splitter was ASCII-only) and silently returned every stored fact.
- **Code in Arabic replies rendered mirrored**, and blockquote indent sat on the wrong side in RTL. Code is now always left-to-right.
- Unknown `session_id` returned 500; now 404. An over-long message showed `[object Object]` in the UI.

### Added
- `/api/health` reports `ready`, missing models, Ollama errors, warm-up state and version; the UI status line uses it and re-checks every 15 seconds.
- Background model warm-up at server start (same `num_ctx` / `keep_alive` as real requests).
- SSE keep-alive comments, bounded concurrent runs, server-side persistence of replies (also on failure).
- `Orchestra.handle(on_event=...)` — the web stream and the CLI now share one pipeline path.
- `python -m orchestra doctor` shares the readiness check and prints the context window.
- Smoke test gains a "plain question must not invent tools" case and an Arabic-in/Arabic-out case, and checks readiness first.
- CI: lint job, least-privilege permissions, cancel superseded runs. `.gitattributes` for consistent line endings.

### Tests
103 → 199.
