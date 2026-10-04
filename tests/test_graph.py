"""Builds the real graph with a fake Gmail service and stubbed sub-agents, then
checks routing and the interrupt/resume flow. No network, no LLM."""

from unittest.mock import MagicMock

import pytest
from googleapiclient.discovery import Resource
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from gmail_agent import graph as graph_mod
from gmail_agent.gmail.client import GmailClient, parse_message
from gmail_agent.schemas import ActionKind, ExtractedEmail, NextAction


@pytest.fixture
def client(raw_message):
    service = MagicMock(spec=Resource)
    client = GmailClient(service)
    client.get_email = lambda mid: parse_message(raw_message)
    client.create_draft = MagicMock(return_value="draft-123")
    client.modify_labels = MagicMock()
    client.archive = MagicMock()
    return client


def _extracted():
    return ExtractedEmail(
        sender_name="Acme Billing", sender_email="billing@acme.example",
        subject="Invoice #4521", summary="Invoice for September.", intent="invoice",
    )


def _build(monkeypatch, client, action: NextAction):
    monkeypatch.setattr(graph_mod, "run_extractor", lambda agent, email: _extracted())
    monkeypatch.setattr(graph_mod, "run_triage", lambda agent, email, ex: action)
    return graph_mod.build_graph(client, checkpointer=InMemorySaver())


def test_label_route(monkeypatch, client):
    g = _build(monkeypatch, client, NextAction(
        kind=ActionKind.label, reasoning="invoice", labels=["Finance"], confidence=0.9))
    cfg = {"configurable": {"thread_id": "t1"}}
    out = g.invoke({"message_id": "18f3a9c0deadbeef"}, config=cfg)
    assert out["outcome"] == "labels applied: ['Finance']"
    client.modify_labels.assert_called_once_with("18f3a9c0deadbeef", add=["Finance"])


def test_archive_route(monkeypatch, client):
    g = _build(monkeypatch, client, NextAction(
        kind=ActionKind.archive, reasoning="promo", confidence=0.8))
    out = g.invoke({"message_id": "x"}, config={"configurable": {"thread_id": "t2"}})
    assert out["outcome"] == "archived"
    client.archive.assert_called_once()


def test_ignore_is_noop(monkeypatch, client):
    g = _build(monkeypatch, client, NextAction(
        kind=ActionKind.ignore, reasoning="nothing", confidence=0.7))
    out = g.invoke({"message_id": "x"}, config={"configurable": {"thread_id": "t3"}})
    assert out["outcome"] == "no action taken (ignore)"


def test_draft_reply_waits_for_approval(monkeypatch, client):
    g = _build(monkeypatch, client, NextAction(
        kind=ActionKind.draft_reply, reasoning="needs answer",
        draft_reply="Thanks, received.", confidence=0.95))
    cfg = {"configurable": {"thread_id": "t4"}}
    out = g.invoke({"message_id": "m"}, config=cfg)
    assert "__interrupt__" in out
    payload = out["__interrupt__"][0].value
    assert payload["type"] == "approve_draft"
    assert payload["body"] == "Thanks, received."
    client.create_draft.assert_not_called()

    out = g.invoke(Command(resume={"approved": True}), config=cfg)
    assert out["outcome"] == "draft created: draft-123"
    client.create_draft.assert_called_once()


def test_draft_reply_rejected(monkeypatch, client):
    g = _build(monkeypatch, client, NextAction(
        kind=ActionKind.draft_reply, reasoning="r", draft_reply="hi", confidence=0.9))
    cfg = {"configurable": {"thread_id": "t5"}}
    g.invoke({"message_id": "m"}, config=cfg)
    out = g.invoke(Command(resume={"approved": False}), config=cfg)
    assert out["outcome"] == "draft rejected by reviewer"
    client.create_draft.assert_not_called()


def test_needs_human_route(monkeypatch, client):
    g = _build(monkeypatch, client, NextAction(
        kind=ActionKind.needs_human, reasoning="legal", confidence=0.4))
    cfg = {"configurable": {"thread_id": "t6"}}
    out = g.invoke({"message_id": "m"}, config=cfg)
    assert out["__interrupt__"][0].value["type"] == "needs_human"
    out = g.invoke(Command(resume="will call them"), config=cfg)
    assert out["outcome"] == "handed to human: will call them"
