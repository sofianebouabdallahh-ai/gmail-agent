"""Runtime settings, loaded from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    triage_model: str = field(default_factory=lambda: _env("TRIAGE_MODEL", "claude-opus-5-5"))
    triage_effort: str = field(default_factory=lambda: _env("TRIAGE_EFFORT", "high"))
    extract_model: str = field(default_factory=lambda: _env("EXTRACT_MODEL", "claude-sonnet-5-5"))
    extract_effort: str = field(default_factory=lambda: _env("EXTRACT_EFFORT", "medium"))
    fallbacks: bool = field(default_factory=lambda: _env("ANTHROPIC_FALLBACKS", "1") == "1")
    # "api": LangChain + ANTHROPIC_API_KEY.  "claude_code": Claude Agent SDK + Claude Code login.
    llm_backend: str = field(default_factory=lambda: _env("LLM_BACKEND", "api"))

    credentials_file: Path = field(
        default_factory=lambda: Path(_env("GMAIL_CREDENTIALS_FILE", "credentials.json"))
    )
    token_file: Path = field(default_factory=lambda: Path(_env("GMAIL_TOKEN_FILE", "token.json")))
    state_db: Path = field(default_factory=lambda: Path(_env("STATE_DB", "state.db")))

    # Max characters of attachment text handed to the extractor per attachment.
    attachment_text_limit: int = 40_000


settings = Settings()
