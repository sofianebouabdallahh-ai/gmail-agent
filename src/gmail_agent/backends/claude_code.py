"""Backend "claude_code": the Claude Agent SDK, using the Claude Code login on this
machine (e.g. a Claude Max subscription) instead of ANTHROPIC_API_KEY. For personal use.

Claude Code's own tools, settings, CLAUDE.md files and native skills are all switched
off: the agent sees exactly the harness prompt and the harness tools, like the api backend.
"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from pydantic import BaseModel, ValidationError

from gmail_agent.harness_loader import AgentSpec
from gmail_agent.schemas import RunUsage
from gmail_agent.tools import ToolSpec

SERVER = "harness"
ATTEMPTS = 2  # one retry on transient CLI/API failures


class ClaudeCodeError(RuntimeError):
    pass


def to_sdk(spec_tool: ToolSpec):
    @tool(spec_tool.name, spec_tool.description, spec_tool.args.model_json_schema())
    async def handler(args: dict) -> dict:
        text = await asyncio.to_thread(spec_tool.run, args)
        return {"content": [{"type": "text", "text": text}]}

    return handler


def _options(spec: AgentSpec, tools: list[ToolSpec]) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=spec.system_prompt,
        model=spec.model,
        effort=spec.effort,
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=[to_sdk(t) for t in tools])},
        tools=[],  # no built-in tools (no Bash, Read, Write, web): only ours
        allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in tools],
        setting_sources=[],  # ignore ~/.claude settings, CLAUDE.md and project config
        output_format={"type": "json_schema", "schema": spec.output.model_json_schema()},
        max_turns=25,
        # An API key in the environment would take precedence over the subscription login.
        env={"ANTHROPIC_API_KEY": ""},
    )


async def _run(spec: AgentSpec, user_message: str, tools: list[ToolSpec]) -> ResultMessage:
    result: ResultMessage | None = None
    async for message in query(prompt=user_message, options=_options(spec, tools)):
        if isinstance(message, ResultMessage):
            result = message
    if result is None or result.is_error or result.structured_output is None:
        detail: Any = (result.errors or result.result) if result else "no result"
        raise ClaudeCodeError(f"{spec.name}: Claude Code run failed: {detail}")
    return result


class ClaudeCodeBackend:
    name = "claude_code"

    def run(self, spec: AgentSpec, user_message: str,
            tools: list[ToolSpec]) -> tuple[BaseModel, RunUsage]:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                result = asyncio.run(_run(spec, user_message, tools))
                output = spec.output.model_validate(result.structured_output)
                break
            except ValidationError:
                raise  # the model returned the wrong shape: retrying will not fix the harness
            except Exception:
                if attempt == ATTEMPTS:
                    raise
        raw = result.usage or {}
        usage = RunUsage(
            agent=spec.name, backend=self.name, model=spec.model,
            input_tokens=int(raw.get("input_tokens", 0)
                             + raw.get("cache_read_input_tokens", 0)
                             + raw.get("cache_creation_input_tokens", 0)),
            output_tokens=int(raw.get("output_tokens", 0)),
            cost_usd=result.total_cost_usd,
        )
        return output, usage

