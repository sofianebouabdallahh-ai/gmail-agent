"""Builds the real graph with a fake Gmail service and a fake backend, then checks
routing, policy, dry-run, the interrupt/resume flow and run recording. No network, no LLM."""

from unittest.mock import MagicMock

import pytest
from googleapiclient.discovery import Resource
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from conftest import FakeBackend, extracted
from gmail_agent.graph import build_graph, run_message
from gmail_agent.gmail.client import GmailClient, parse_message
from gmail_agent.schemas import ActionKind, NextAction
from gmail_agent.store import ProcessedStore


@pytest.fixture
def client(raw_message):
    service = MagicMock(spec=Resource)
    client = GmailClient(service)
    client.get_email = lambda mid: parse_message(raw_message)
    client.create_draft = MagicMock(return_value="draft-123")
    client.modify_labels = MagicMock()
    client.archive = MagicMock()
    return client


def _graph(client, action: NextAction):
    backend = FakeBackend({"extractor": extracted(), "triage": action})
    return build_graph(client, backend=backend, checkpointer=InMemorySaver()), backend


def _cfg(t):
    return {"configurable": {"thread_id": t}}


def test_agents_get_their_tools_and_prompts(client):
    g, backend = _graph(client, NextAction(kind=ActionKind.ignore, reasoning="r", confidence=0.9))
    g.invoke({"message_id": "m"}, config=_cfg("t0"))
    (ext_name, ext_msg, ext_tools), (tri_name, tri_msg, tri_tools) = backend.calls
    assert ext_name == "extractor" and "untrusted" in ext_msg
    assert ext_tools == ["read_attachment", "load_skill", "read_skill_file"]
    assert tri_name == "triage" and "## Extraction" in tri_msg
    assert tri_tools == ["search_gmail", "get_gmail_thread", "load_skill", "read_skill_file"]


def test_label_route(client):
    g, _ = _graph(client, NextAction(kind=ActionKind.label, reasoning="invoice",
                                     labels=["finance"], confidence=0.9))
    out = g.invoke({"message_id": "18f3a9c0deadbeef"}, config=_cfg("t1"))
    assert out["outcome"] == "labels applied: ['Finance']"  # normalised by policy
    client.modify_labels.assert_called_once_with("18f3a9c0deadbeef", add=["Finance"])
    assert set(out["usage"]) == {"extractor", "triage"}


def test_archive_route(client):
    g, _ = _graph(client, NextAction(kind=ActionKind.archive, reasoning="promo", confidence=0.8))
    out = g.invoke({"message_id": "x"}, config=_cfg("t2"))
    assert out["outcome"] == "archived"
    client.archive.assert_called_once()


def test_ignore_is_noop(client):
    g, _ = _graph(client, NextAction(kind=ActionKind.ignore, reasoning="nothing", confidence=0.7))
    out = g.invoke({"message_id": "x"}, config=_cfg("t3"))
    assert out["outcome"] == "no action taken (ignore)"


def test_dry_run_never_writes_to_gmail(client):
    for i, action in enumerate([
        NextAction(kind=ActionKind.archive, reasoning="r", confidence=0.9),
        NextAction(kind=ActionKind.label, reasoning="r", labels=["Travel"], confidence=0.9),
    ]):
        g, _ = _graph(client, action)
        out = g.invoke({"message_id": "x", "dry_run": True}, config=_cfg(f"dry{i}"))
        assert out["outcome"].startswith("dry-run: would")
    g, _ = _graph(client, NextAction(kind=ActionKind.draft_reply, reasoning="r",
                                     draft_reply="Hi", confidence=0.9))
    g.invoke({"message_id": "x", "dry_run": True}, config=_cfg("dry-draft"))
    out = g.invoke(Command(resume={"approved": True}), config=_cfg("dry-draft"))
    assert out["outcome"] == "dry-run: would create a draft reply"
    client.archive.assert_not_called()
    client.modify_labels.assert_not_called()
    client.create_draft.assert_not_called()


def test_low_confidence_is_overridden_to_human(client):
    g, _ = _graph(client, NextAction(kind=ActionKind.archive, reasoning="maybe", confidence=0.3))
    out = g.invoke({"message_id": "m"}, config=_cfg("t-low"))
    assert out["proposed"].kind == ActionKind.archive
    assert out["action"].kind == ActionKind.needs_human
    assert "confidence" in out["policy_notes"][0]
    assert out["__interrupt__"][0].value["type"] == "needs_human"
    client.archive.assert_not_called()


def test_draft_reply_waits_for_approval(client):
    g, _ = _graph(client, NextAction(kind=ActionKind.draft_reply, reasoning="needs answer",
                                     draft_reply="Thanks, received.", confidence=0.95))
    out = g.invoke({"message_id": "m"}, config=_cfg("t4"))
    payload = out["__interrupt__"][0].value
    assert payload["type"] == "approve_draft" and payload["body"] == "Thanks, received."
    client.create_draft.assert_not_called()

    out = g.invoke(Command(resume={"approved": True, "body": "Edited"}), config=_cfg("t4"))
    assert out["outcome"] == "draft created: draft-123"
    assert client.create_draft.call_args.kwargs["body"] == "Edited"


def test_draft_reply_rejected(client):
    g, _ = _graph(client, NextAction(kind=ActionKind.draft_reply, reasoning="r",
                                     draft_reply="hi", confidence=0.9))
    g.invoke({"message_id": "m"}, config=_cfg("t5"))
    out = g.invoke(Command(resume={"approved": False}), config=_cfg("t5"))
    assert out["outcome"] == "draft rejected by reviewer"
    client.create_draft.assert_not_called()


def test_needs_human_route(client):
    g, _ = _graph(client, NextAction(kind=ActionKind.needs_human, reasoning="legal", confidence=0.4))
    out = g.invoke({"message_id": "m"}, config=_cfg("t6"))
    assert out["__interrupt__"][0].value["type"] == "needs_human"
    out = g.invoke(Command(resume="will call them"), config=_cfg("t6"))
    assert out["outcome"] == "handed to human: will call them"


def test_run_message_records_waiting_then_done(client, tmp_path):
    store = ProcessedStore(tmp_path / "s.db")
    g, _ = _graph(client, NextAction(kind=ActionKind.draft_reply, reasoning="r",
                                     draft_reply="hi", confidence=0.9))
    r = run_message(g, store, "m1", backend_name="fake")
    assert r.status == "waiting" and r.interrupt["type"] == "approve_draft"
    assert store.status("m1") == "waiting"
    r = run_message(g, store, "m1", resume={"approved": True}, backend_name="fake")
    assert r.status == "done" and store.status("m1") == "done"
    row = store.all()["m1"]
    assert row["cost_usd"] == pytest.approx(0.02)


def test_run_message_records_failure(client, tmp_path):
    store = ProcessedStore(tmp_path / "s.db")
    backend = FakeBackend({"extractor": RuntimeError("model overloaded"), "triage": None})
    g = build_graph(client, backend=backend, checkpointer=InMemorySaver())
    r = run_message(g, store, "m2")
    assert r.status == "failed" and "model overloaded" in r.error
    assert store.status("m2") == "failed"
    assert store.seen("m2") and not store.seen("m2", retry_failed=True)
