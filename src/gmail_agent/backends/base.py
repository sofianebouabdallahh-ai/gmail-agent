"""The interface every LLM backend implements."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from gmail_agent.harness_loader import AgentSpec
from gmail_agent.schemas import RunUsage
from gmail_agent.tools import ToolSpec


class Backend(Protocol):
    name: str

    def run(self, spec: AgentSpec, user_message: str,
            tools: list[ToolSpec]) -> tuple[BaseModel, RunUsage]:
        """Run one agent to completion and return its validated output (an instance of
        `spec.output`) and token/cost usage. Duration and tool calls are filled in by
        `run_agent`."""
        ...
