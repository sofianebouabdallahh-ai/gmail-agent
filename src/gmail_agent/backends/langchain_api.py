"""Backend "api": LangChain `create_agent` + ChatAnthropic, billed to ANTHROPIC_API_KEY.

Notes that matter for the Claude 5.5 family:
- thinking is always on; `output_config.effort` controls depth.
- do not set temperature / top_p / top_k; non-default sampling params are rejected.
- forced tool_choice is rejected, so structured output uses the provider-native strategy
  (create_agent picks it automatically for Claude when given a Pydantic class).
- server-side refusal fallbacks route a safety-classifier refusal to another model
  instead of returning an empty answer (ANTHROPIC_FALLBACKS).
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from gmail_agent.config import settings
from gmail_agent.harness_loader import AgentSpec
from gmail_agent.schemas import RunUsage
from gmail_agent.tools import ToolSpec


def make_model(model_id: str, effort: str, max_tokens: int = 16_000) -> ChatAnthropic:
    kwargs: dict = dict(
        model=model_id,
        max_tokens=max_tokens,
        max_retries=3,  # 429 / 5xx / overloaded, with backoff
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
    )
    if settings.fallbacks:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["model_kwargs"] = {"fallbacks": "default"}
    return ChatAnthropic(**kwargs)


def to_langchain(tool: ToolSpec) -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda **kwargs: tool.run(kwargs),
        name=tool.name,
        description=tool.description,
        args_schema=tool.args,
    )


class LangChainBackend:
    name = "api"

    def run(self, spec: AgentSpec, user_message: str,
            tools: list[ToolSpec]) -> tuple[BaseModel, RunUsage]:
        agent = create_agent(
            model=make_model(spec.model, spec.effort),
            tools=[to_langchain(t) for t in tools],
            system_prompt=spec.system_prompt,
            response_format=spec.output,
            name=spec.name,
        )
        result = agent.invoke({"messages": [{"role": "user", "content": user_message}]})
        usage = RunUsage(agent=spec.name, backend=self.name, model=spec.model)
        for m in result["messages"]:
            if isinstance(m, AIMessage) and m.usage_metadata:
                usage.input_tokens += m.usage_metadata.get("input_tokens", 0)
                usage.output_tokens += m.usage_metadata.get("output_tokens", 0)
        return result["structured_response"], usage
