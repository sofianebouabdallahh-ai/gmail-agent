"""LLM backends and the one function the pipeline uses to run an agent."""

from __future__ import annotations

import time

from pydantic import BaseModel

from gmail_agent.backends.base import Backend
from gmail_agent.harness_loader import AgentSpec
from gmail_agent.schemas import RunUsage
from gmail_agent.tools import build_tools

BACKENDS = ("api", "claude_code")


def get_backend(name: str) -> Backend:
    # Imported lazily: each backend pulls in its own heavy SDK.
    if name == "api":
        from gmail_agent.backends.langchain_api import LangChainBackend

        return LangChainBackend()
    if name == "claude_code":
        from gmail_agent.backends.claude_code import ClaudeCodeBackend

        return ClaudeCodeBackend()
    raise ValueError(f"unknown LLM_BACKEND {name!r}; expected one of {BACKENDS}")


def run_agent(backend: Backend, spec: AgentSpec, client, user_message: str) -> tuple[BaseModel, RunUsage]:
    """Run `spec` with its tools bound to `client`; fill in duration and the tool trace."""
    trace: list[str] = []
    started = time.monotonic()
    output, usage = backend.run(spec, user_message, build_tools(spec, client, trace))
    usage.duration_ms = int((time.monotonic() - started) * 1000)
    usage.tool_calls = trace
    return output, usage


__all__ = ["BACKENDS", "Backend", "get_backend", "run_agent"]
