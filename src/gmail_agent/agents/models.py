"""Factory for Claude chat models configured for the Claude 5.5 family.

Notes that matter for Opus 5.5 / Sonnet 5.5:
- thinking is always on; use `output_config.effort` to control depth (Opus defaults to medium).
- do not set temperature / top_p / top_k; non-default sampling params are rejected.
- forced tool_choice is rejected, so structured output must use the provider-native strategy
  (create_agent picks that automatically for Claude when given a Pydantic class).
- server-side refusal fallbacks route a safety-classifier refusal to another model instead of
  returning an empty answer. Controlled by ANTHROPIC_FALLBACKS.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic

from gmail_agent.config import settings


def make_model(model_id: str, effort: str, max_tokens: int = 16_000) -> ChatAnthropic:
    kwargs: dict = dict(
        model=model_id,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
    )
    if settings.fallbacks:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["model_kwargs"] = {"fallbacks": "default"}
    return ChatAnthropic(**kwargs)


def triage_model() -> ChatAnthropic:
    return make_model(settings.triage_model, settings.triage_effort)


def extract_model() -> ChatAnthropic:
    return make_model(settings.extract_model, settings.extract_effort)
