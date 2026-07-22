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
    specialist_temperature: float = 0.2
    request_timeout_s: float = 120.0

    # ── Execution limits (machine-aware) ───────────────────────────
    # Ollama serializes GPU/CPU inference anyway; 2 concurrent requests
    # lets I/O overlap without thrashing a laptop.
    max_concurrency: int = Field(2, ge=1, le=8)
    max_specialist_steps: int = Field(6, ge=1, le=20)
    max_retries: int = Field(2, ge=0, le=5)
    retry_backoff_s: float = 1.5

    # ── Persistence ────────────────────────────────────────────────
    data_dir: Path = Path.home() / ".orchestra"
    thread_id: str = "default"

    # ── Observability ──────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False  # True -> machine-readable logs for analysis

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
