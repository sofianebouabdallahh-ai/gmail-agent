"""Runtime settings, loaded from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in _env(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # "api": LangChain + ANTHROPIC_API_KEY.  "claude_code": Claude Agent SDK + Claude Code login.
    llm_backend: str = field(default_factory=lambda: _env("LLM_BACKEND", "api"))
    triage_model: str = field(default_factory=lambda: _env("TRIAGE_MODEL", "claude-opus-5-5"))
    triage_effort: str = field(default_factory=lambda: _env("TRIAGE_EFFORT", "high"))
    extract_model: str = field(default_factory=lambda: _env("EXTRACT_MODEL", "claude-sonnet-5-5"))
    extract_effort: str = field(default_factory=lambda: _env("EXTRACT_EFFORT", "medium"))
    fallbacks: bool = field(default_factory=lambda: _env_bool("ANTHROPIC_FALLBACKS", True))

    # Guardrails enforced in code (gmail_agent.policy), whatever the model says.
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))
    min_confidence: float = field(default_factory=lambda: float(_env("MIN_CONFIDENCE", "0.6")))
    allowed_labels: tuple[str, ...] = field(default_factory=lambda: _env_list(
        "ALLOWED_LABELS",
        "Finance,Receipts,Travel,Insurance,Health,Work,Shopping,Newsletters,Calendar,Personal",
    ))

    credentials_file: Path = field(
        default_factory=lambda: Path(_env("GMAIL_CREDENTIALS_FILE", "credentials.json"))
    )
    token_file: Path = field(default_factory=lambda: Path(_env("GMAIL_TOKEN_FILE", "token.json")))
    state_db: Path = field(default_factory=lambda: Path(_env("STATE_DB", "state.db")))
    # Optional owner profile appended to every agent prompt (name, languages, preferences).
    owner_file: Path = field(default_factory=lambda: Path(_env("OWNER_FILE", "owner.md")))

    # Max characters of text any tool hands back to a model (attachments, threads, skills).
    attachment_text_limit: int = 40_000
    # Gmail API retries on 429/5xx (exponential backoff built into googleapiclient).
    gmail_retries: int = 3


settings = Settings()
