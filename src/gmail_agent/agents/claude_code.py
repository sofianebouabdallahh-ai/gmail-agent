"""Alternative backend: run the two sub-agents through Claude Code (Claude Agent SDK).

Uses the Claude Code login on this machine (e.g. a Claude Max subscription) instead of
ANTHROPIC_API_KEY. Same prompts, same tools, same Pydantic outputs as the LangChain
agents, so the graph does not care which backend produced them. For personal use.

Selected with LLM_BACKEND=claude_code.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from langchain_google_community import GmailToolkit
from pydantic import BaseModel

from gmail_agent.agents.attachments import attachment_to_text
from gmail_agent.agents.extractor import SYSTEM_PROMPT as EXTRACT_PROMPT
from gmail_agent.agents.triage import SYSTEM_PROMPT as TRIAGE_PROMPT
from gmail_agent.agents.triage import TRIAGE_TOOL_NAMES, triage_user_message
from gmail_agent.config import settings
from gmail_agent.gmail import GmailClient
from gmail_agent.schemas import Email, ExtractedEmail, NextAction

SERVER = "gmail"


def _text(value: Any) -> dict:
    if not isinstance(value, str):
        value = json.dumps(value, default=str)
    return {"content": [{"type": "text", "text": value}]}


def _options(system_prompt: str, tools: list, model: str, effort: str,
             output: type[BaseModel]) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        effort=effort,
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=tools)},
        tools=[],  # no built-in tools (no Bash, Read, Write, web): only ours
        allowed_tools=[f"mcp__{SERVER}__{t.name}" for t in tools],
        setting_sources=[],  # ignore ~/.claude settings, CLAUDE.md and project config
        output_format={"type": "json_schema", "schema": output.model_json_schema()},
        max_turns=20,
        # An API key in the environment would take precedence over the subscription login.
        env={"ANTHROPIC_API_KEY": ""},
    )


async def _run(prompt: str, options: ClaudeAgentOptions, output: type[BaseModel]):
    result: ResultMessage | None = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None or result.is_error or result.structured_output is None:
        detail = (result.errors or result.result) if result else "no result"
        raise RuntimeError(f"Claude Code run failed: {detail}")
    return output.model_validate(result.structured_output)


# --------------------------------------------------------------------------- #
# Extractor
# --------------------------------------------------------------------------- #
def build_extractor(client: GmailClient) -> ClaudeAgentOptions:
    @tool(
        "read_attachment",
        "Download one attachment of the given message and return its text content. "
        "Pass the message_id, attachment_id, filename and mime_type exactly as listed in the email.",
        {"message_id": str, "attachment_id": str, "filename": str, "mime_type": str},
    )
    async def read_attachment(args: dict) -> dict:
        data = await asyncio.to_thread(
            client.download_attachment, args["message_id"], args["attachment_id"]
        )
        return _text(attachment_to_text(data, args["mime_type"], args["filename"]))

    return _options(EXTRACT_PROMPT, [read_attachment], settings.extract_model,
                    settings.extract_effort, ExtractedEmail)


def run_extractor(options: ClaudeAgentOptions, email: Email) -> ExtractedEmail:
    return asyncio.run(_run(email.as_prompt_text(), options, ExtractedEmail))


# --------------------------------------------------------------------------- #
# Triage
# --------------------------------------------------------------------------- #
def _wrap_langchain_tool(lc_tool):
    @tool(lc_tool.name, lc_tool.description, lc_tool.args_schema.model_json_schema())
    async def handler(args: dict) -> dict:
        return _text(await asyncio.to_thread(lc_tool.invoke, args))

    return handler


def build_triage(client: GmailClient) -> ClaudeAgentOptions:
    toolkit = GmailToolkit(api_resource=client.service)
    tools = [_wrap_langchain_tool(t) for t in toolkit.get_tools() if t.name in TRIAGE_TOOL_NAMES]
    return _options(TRIAGE_PROMPT, tools, settings.triage_model, settings.triage_effort,
                    NextAction)


def run_triage(options: ClaudeAgentOptions, email: Email, extracted: ExtractedEmail) -> NextAction:
    return asyncio.run(_run(triage_user_message(email, extracted), options, NextAction))
