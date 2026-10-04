"""The harness: a LangGraph StateGraph that runs ingest -> extract -> decide -> guard -> act.

The graph is the "main agent". `extract` and `decide` run sub-agents defined in
harness/agents/*.md through the configured backend; `guard` applies code-enforced policy;
everything else is plain Python. Outbound actions pause with `interrupt` so a human
approves them, and in dry-run mode they only describe what they would do.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from gmail_agent import logs, policy
from gmail_agent.backends import Backend, get_backend, run_agent
from gmail_agent.config import settings
from gmail_agent.harness_loader import load_agent
from gmail_agent.schemas import (
    ActionKind,
    Attachment,
    Email,
    ExtractedAttachment,
    ExtractedEmail,
    NextAction,
    Priority,
    RunUsage,
)
from gmail_agent.store import ProcessedStore


class PipelineState(TypedDict, total=False):
    message_id: str
    dry_run: bool
    email: Email
    extracted: ExtractedEmail
    proposed: NextAction  # what the triage agent said
    action: NextAction  # what policy allowed (what actually runs)
    policy_notes: list[str]
    usage: dict[str, RunUsage]
    outcome: str


def triage_message(email: Email, extracted: ExtractedEmail) -> str:
    return (
        "## Extraction\n"
        f"{extracted.model_dump_json(indent=2)}\n\n"
        "## Raw headers\n"
        f"From: {email.sender}\nTo: {', '.join(email.to)}\nDate: {email.date}\n"
        f"Subject: {email.subject}\nThread-Id: {email.thread_id}\nLabels: {', '.join(email.labels)}"
    )


def build_graph(client, backend: Backend | None = None, checkpointer=None):
    backend = backend or get_backend(settings.llm_backend)
    extractor = load_agent("extractor")  # validated now: a bad AGENT.md fails at startup
    triage = load_agent("triage")

    # ---- nodes ------------------------------------------------------------ #
    def ingest(state: PipelineState) -> PipelineState:
        return {"email": client.get_email(state["message_id"])}

    def extract(state: PipelineState) -> PipelineState:
        out, usage = run_agent(backend, extractor, client, state["email"].as_prompt_text())
        return {"extracted": out, "usage": {**state.get("usage", {}), "extractor": usage}}

    def decide(state: PipelineState) -> PipelineState:
        msg = triage_message(state["email"], state["extracted"])
        out, usage = run_agent(backend, triage, client, msg)
        return {"proposed": out, "usage": {**state.get("usage", {}), "triage": usage}}

    def guard(state: PipelineState) -> PipelineState:
        action, notes = policy.enforce(state["proposed"], min_confidence=settings.min_confidence,
                                       allowed_labels=settings.allowed_labels)
        return {"action": action, "policy_notes": notes}

    def draft_reply(state: PipelineState) -> PipelineState:
        email, action = state["email"], state["action"]
        approval = interrupt(
            {
                "type": "approve_draft",
                "to": email.sender,
                "subject": f"Re: {email.subject}",
                "body": action.draft_reply,
                "reasoning": action.reasoning,
            }
        )
        if not approval.get("approved"):
            return {"outcome": "draft rejected by reviewer"}
        body = approval.get("body") or action.draft_reply or ""
        if state.get("dry_run"):
            return {"outcome": "dry-run: would create a draft reply"}
        draft_id = client.create_draft(
            to=email.sender, subject=f"Re: {email.subject}", body=body, thread_id=email.thread_id
        )
        return {"outcome": f"draft created: {draft_id}"}

    def apply_labels(state: PipelineState) -> PipelineState:
        labels = state["action"].labels
        if state.get("dry_run"):
            return {"outcome": f"dry-run: would apply labels {labels}"}
        client.modify_labels(state["message_id"], add=labels)
        return {"outcome": f"labels applied: {labels}"}

    def archive(state: PipelineState) -> PipelineState:
        if state.get("dry_run"):
            return {"outcome": "dry-run: would archive"}
        client.archive(state["message_id"])
        return {"outcome": "archived"}

    def needs_human(state: PipelineState) -> PipelineState:
        note = interrupt(
            {
                "type": "needs_human",
                "subject": state["email"].subject,
                "reasoning": state["action"].reasoning,
                "priority": state["action"].priority.value,
            }
        )
        return {"outcome": f"handed to human: {note}"}

    def noop(state: PipelineState) -> PipelineState:
        kind = state["action"].kind.value
        return {"outcome": f"no action taken ({kind})"}

    # ---- routing ---------------------------------------------------------- #
    def route(state: PipelineState) -> Literal["draft_reply", "apply_labels", "archive", "needs_human", "noop"]:
        match state["action"].kind:
            case ActionKind.draft_reply:
                return "draft_reply"
            case ActionKind.label:
                return "apply_labels"
            case ActionKind.archive:
                return "archive"
            case ActionKind.needs_human:
                return "needs_human"
            case _:  # ignore (schedule is turned into needs_human by policy)
                return "noop"

    g = StateGraph(PipelineState)
    for name, fn in [("ingest", ingest), ("extract", extract), ("decide", decide),
                     ("guard", guard), ("draft_reply", draft_reply),
                     ("apply_labels", apply_labels), ("archive", archive),
                     ("needs_human", needs_human), ("noop", noop)]:
        g.add_node(name, fn)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "extract")
    g.add_edge("extract", "decide")
    g.add_edge("decide", "guard")
    g.add_conditional_edges("guard", route)
    for n in ("draft_reply", "apply_labels", "archive", "needs_human", "noop"):
        g.add_edge(n, END)

    return g.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# Running one message: shared by the CLI, the poller and the web UI
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    status: Literal["done", "waiting", "failed"]
    values: dict = field(default_factory=dict)
    interrupt: dict | None = None  # payload to show the human when status == "waiting"
    error: str | None = None


_START = object()


def run_message(graph, store: ProcessedStore, message_id: str, *, resume: Any = _START,
                dry_run: bool = False, backend_name: str | None = None) -> RunResult:
    """Start (or resume after an approval) the pipeline for one message, record the
    result in `store`, and log one structured line. Never raises for a pipeline error."""
    config = {"configurable": {"thread_id": f"msg-{message_id}"}}
    try:
        if resume is _START:
            graph.invoke({"message_id": message_id, "dry_run": dry_run}, config=config)
        else:
            graph.invoke(Command(resume=resume), config=config)
        state = graph.get_state(config)
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        store.mark(message_id, status="failed", error=error[:2000], backend=backend_name)
        logs.event("run_failed", message_id=message_id, error=error[:500])
        return RunResult("failed", error=error)

    values = state.values
    action: NextAction | None = values.get("action")
    usage: dict[str, RunUsage] = values.get("usage", {})
    costs = [u.cost_usd for u in usage.values() if u.cost_usd is not None]
    record = dict(
        action_kind=action.kind.value if action else None,
        action=action.model_dump(mode="json") if action else None,
        cost_usd=round(sum(costs), 6) if costs else None,
        duration_ms=sum(u.duration_ms for u in usage.values()) or None,
        backend=backend_name,
    )
    if state.next:  # paused at an interrupt: a human has to answer
        pending = state.tasks[0].interrupts if state.tasks else ()
        store.mark(message_id, status="waiting", **record)
        logs.event("run_waiting", message_id=message_id, action=record["action_kind"],
                   policy_notes=values.get("policy_notes") or None)
        return RunResult("waiting", values, pending[0].value if pending else None)

    # A dry-run did not change Gmail, so it must not stop a later real run.
    status = "dry_run" if values.get("dry_run") else "done"
    store.mark(message_id, status=status, **record)
    logs.event(
        "run_done", message_id=message_id, dry_run=values.get("dry_run") or None,
        proposed=values["proposed"].kind.value if values.get("proposed") else None,
        action=record["action_kind"], policy_notes=values.get("policy_notes") or None,
        outcome=values.get("outcome"), cost_usd=record["cost_usd"],
        duration_ms=record["duration_ms"],
        tools={k: u.tool_calls for k, u in usage.items()} or None,
    )
    return RunResult("done", values)


def sqlite_checkpointer() -> SqliteSaver:
    conn = sqlite3.connect(settings.state_db, check_same_thread=False)
    # Our own types are stored in checkpoints; register them so loading is not blocked.
    schema_types = (Email, Attachment, ExtractedEmail, ExtractedAttachment, NextAction,
                    ActionKind, Priority, RunUsage)
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[(t.__module__, t.__name__) for t in schema_types]
    )
    return SqliteSaver(conn, serde=serde)
