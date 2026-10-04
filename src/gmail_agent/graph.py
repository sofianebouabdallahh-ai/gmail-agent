"""The harness: a LangGraph StateGraph that runs ingest -> extract -> triage -> act.

The graph is the "main agent". Nodes that need judgment call a sub-agent built with
create_agent; everything else is plain Python. Outbound actions pause with `interrupt`
so a human approves them (the CLI handles the resume).
"""

from __future__ import annotations

import sqlite3
from typing import Literal, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from gmail_agent.config import settings

if settings.llm_backend == "claude_code":
    from gmail_agent.agents.claude_code import (
        build_extractor,
        build_triage,
        run_extractor,
        run_triage,
    )
else:
    from gmail_agent.agents.extractor import build_extractor, run_extractor
    from gmail_agent.agents.triage import build_triage, run_triage
from gmail_agent.gmail import GmailClient
from gmail_agent.schemas import (
    ActionKind,
    Attachment,
    Email,
    ExtractedAttachment,
    ExtractedEmail,
    NextAction,
    Priority,
)


class PipelineState(TypedDict, total=False):
    message_id: str
    email: Email
    extracted: ExtractedEmail
    action: NextAction
    outcome: str


def build_graph(client: GmailClient, checkpointer=None):
    extractor = build_extractor(client)
    triage = build_triage(client)

    # ---- nodes ------------------------------------------------------------ #
    def ingest(state: PipelineState) -> PipelineState:
        return {"email": client.get_email(state["message_id"])}

    def extract(state: PipelineState) -> PipelineState:
        return {"extracted": run_extractor(extractor, state["email"])}

    def decide(state: PipelineState) -> PipelineState:
        return {"action": run_triage(triage, state["email"], state["extracted"])}

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
        draft_id = client.create_draft(
            to=email.sender, subject=f"Re: {email.subject}", body=body, thread_id=email.thread_id
        )
        return {"outcome": f"draft created: {draft_id}"}

    def apply_labels(state: PipelineState) -> PipelineState:
        labels = state["action"].labels
        if labels:
            client.modify_labels(state["message_id"], add=labels)
        return {"outcome": f"labels applied: {labels}"}

    def archive(state: PipelineState) -> PipelineState:
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
            case _:  # schedule, ignore
                return "noop"

    g = StateGraph(PipelineState)
    g.add_node("ingest", ingest)
    g.add_node("extract", extract)
    g.add_node("decide", decide)
    g.add_node("draft_reply", draft_reply)
    g.add_node("apply_labels", apply_labels)
    g.add_node("archive", archive)
    g.add_node("needs_human", needs_human)
    g.add_node("noop", noop)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "extract")
    g.add_edge("extract", "decide")
    g.add_conditional_edges("decide", route)
    for n in ("draft_reply", "apply_labels", "archive", "needs_human", "noop"):
        g.add_edge(n, END)

    return g.compile(checkpointer=checkpointer)


def sqlite_checkpointer() -> SqliteSaver:
    conn = sqlite3.connect(settings.state_db, check_same_thread=False)
    # Our own types are stored in checkpoints; register them so loading is not blocked.
    schema_types = (Email, Attachment, ExtractedEmail, ExtractedAttachment, NextAction,
                    ActionKind, Priority)
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[(t.__module__, t.__name__) for t in schema_types]
    )
    return SqliteSaver(conn, serde=serde)
