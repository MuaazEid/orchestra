"""Central configuration — single source of truth.

Design decisions (target machine: Muaaz's Linux Mint laptop, runs 7B models):
- Everything overridable via environment variables (ORCHESTRA_ prefix) or .env
- Model tiering built in from day one: fast tier for trivial work, main tier
  for real work. Both default to what the target machine proved it can run.
- Validated at startup with pydantic -> misconfiguration fails LOUDLY at boot,
  never silently at task #47.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM backend ────────────────────────────────────────────────
    llm_backend: Literal["ollama", "mock"] = "ollama"
    ollama_base_url: str = "http://localhost:11434"

    # Model tiers — the frugal-architecture core.
    # main: reasoning / planning / generation (proved on target machine)
    # fast: classification / extraction / yes-no (pull qwen2.5:3b — optional)
    main_model: str = "qwen2.5:7b"
    fast_model: str = "qwen2.5:7b"   # set to qwen2.5:3b after pulling it
    planner_temperature: float = 0.0
    # Skip the planner LLM call when the message shows no sign of needing
    # any specialist (see engine/guardrails.py). Turn off if you add
    # specialists for languages the keyword signals do not cover.
    planner_fast_path: bool = True
    specialist_temperature: float = 0.2
    request_timeout_s: float = 120.0
    # Context window requested from Ollama on every call. Ollama's own
    # default is 4096 tokens and it truncates silently beyond that, so we
    # ask for an explicit window instead of trusting the default.
    num_ctx: int = Field(8192, ge=1024)
    # How long Ollama keeps a model in memory after its last request.
    keep_alive: str = "30m"
    # Load the models in the background when the web server starts, so the
    # first request does not pay the model-load cost.
    warm_on_start: bool = True

    # ── Execution limits (machine-aware) ───────────────────────────
    # Ollama serializes GPU/CPU inference anyway; 2 concurrent requests
    # lets I/O overlap without thrashing a laptop.
    max_concurrency: int = Field(2, ge=1, le=8)
    max_specialist_steps: int = Field(6, ge=1, le=20)
    max_retries: int = Field(2, ge=0, le=5)
    retry_backoff_s: float = 1.5
    # Pipelines the web server runs at once; extra requests wait their turn.
    max_concurrent_runs: int = Field(2, ge=1, le=8)
    # Upper bound on one chat message (characters). Keeps a pasted novel
    # from blowing up a small model's context.
    max_message_chars: int = Field(8000, ge=100)
    # fetch_webpage refuses loopback / private / link-local addresses unless
    # this is set (it protects services on your own machine and network).
    allow_private_fetch: bool = False

    # ── Persistence ────────────────────────────────────────────────
    data_dir: Path = Path.home() / ".orchestra"
    thread_id: str = "default"

    # ── Observability ──────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False  # True -> machine-readable logs for analysis

    # ── Optional real web search (Tavily) ──────────────────────────
    # Never hardcode this. Set ORCHESTRA_TAVILY_API_KEY in your own local
    # .env — Claude (or anyone reading this source) never sees the value.
    tavily_api_key: str | None = None

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def memory_db(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def metrics_db(self) -> Path:
        return self.data_dir / "metrics.db"

    @field_validator("data_dir")
    @classmethod
    def _ensure_dir(cls, v: Path) -> Path:
        v.mkdir(parents=True, exist_ok=True)
        return v


settings = Settings()
