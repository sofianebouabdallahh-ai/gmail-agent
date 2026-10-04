"""Command line entry point.

  gmail-agent auth                  # run the OAuth flow once, writes token.json
  gmail-agent fetch <message_id>    # parse one email, no LLM
  gmail-agent dump <message_id> f   # save raw JSON as a test fixture
  gmail-agent process <message_id>  # run the full graph on one email
  gmail-agent poll [--query Q] [--max N] [--loop --interval S]
  gmail-agent ui [--port P] [--query Q] [--max N] [--all]  # emails with attachments + results
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from langgraph.types import Command

from gmail_agent.config import settings
from gmail_agent.gmail import GmailClient
from gmail_agent.gmail.auth import get_gmail_service
from gmail_agent.store import ProcessedStore


def _client() -> GmailClient:
    return GmailClient(get_gmail_service())


def cmd_auth(_: argparse.Namespace) -> None:
    svc = get_gmail_service()
    profile = svc.users().getProfile(userId="me").execute()
    print(f"Authenticated as {profile['emailAddress']}; token saved to {settings.token_file}")


def cmd_fetch(args: argparse.Namespace) -> None:
    email = _client().get_email(args.message_id)
    print(email.as_prompt_text())


def cmd_dump(args: argparse.Namespace) -> None:
    raw = _client().get_raw_message(args.message_id)
    Path(args.path).write_text(json.dumps(raw, indent=2))
    print(f"wrote {args.path}")


def _handle_interrupts(graph, config) -> None:
    """Loop until the graph finishes, answering interrupts from the terminal."""
    while True:
        state = graph.get_state(config)
        if not state.next:
            return
        pending = state.tasks[0].interrupts if state.tasks else ()
        if not pending:
            return
        payload = pending[0].value
        if payload.get("type") == "approve_draft":
            print("\n--- Proposed draft ---")
            print(f"To: {payload['to']}\nSubject: {payload['subject']}\n\n{payload['body']}")
            print(f"\nWhy: {payload['reasoning']}")
            ans = input("Save this draft to Gmail? [y/N] ").strip().lower()
            resume = {"approved": ans == "y"}
        else:
            print("\n--- Needs your attention ---")
            print(json.dumps(payload, indent=2))
            resume = input("Note for the record (enter to skip): ").strip() or "acknowledged"
        graph.invoke(Command(resume=resume), config=config)


def _process_one(graph, store: ProcessedStore, message_id: str) -> None:
    from gmail_agent.graph import PipelineState  # noqa: F401  (type reference)

    config = {"configurable": {"thread_id": f"msg-{message_id}"}}
    graph.invoke({"message_id": message_id}, config=config)
    _handle_interrupts(graph, config)
    final = graph.get_state(config).values
    action = final.get("action")
    print(f"\n[{message_id}] {final.get('email').subject if final.get('email') else ''}")
    if action:
        print(f"  action={action.kind.value} priority={action.priority.value} "
              f"confidence={action.confidence:.2f}")
        print(f"  reasoning: {action.reasoning}")
    print(f"  outcome: {final.get('outcome')}")
    store.mark(message_id, action.kind.value if action else None,
               action.model_dump(mode="json") if action else None)


def cmd_process(args: argparse.Namespace) -> None:
    from gmail_agent.graph import build_graph, sqlite_checkpointer

    client = _client()
    graph = build_graph(client, checkpointer=sqlite_checkpointer())
    store = ProcessedStore(settings.state_db)
    _process_one(graph, store, args.message_id)


def cmd_poll(args: argparse.Namespace) -> None:
    from gmail_agent.graph import build_graph, sqlite_checkpointer

    client = _client()
    graph = build_graph(client, checkpointer=sqlite_checkpointer())
    store = ProcessedStore(settings.state_db)
    while True:
        ids = [m for m in client.list_message_ids(args.query, args.max) if not store.seen(m)]
        print(f"{len(ids)} new message(s)")
        for mid in ids:
            try:
                _process_one(graph, store, mid)
            except Exception as exc:  # keep the loop alive; one bad email must not stop polling
                print(f"[{mid}] failed: {exc!r}", file=sys.stderr)
        if not args.loop:
            return
        time.sleep(args.interval)


def cmd_ui(args: argparse.Namespace) -> None:
    from gmail_agent.graph import sqlite_checkpointer
    from gmail_agent.ui import serve

    try:
        client, run_client = _client(), _client()  # one connection per thread of work
    except Exception as exc:  # no credentials yet: still show processed emails
        print(f"Gmail not connected ({exc}); showing processed emails only.")
        client = run_client = None
    query = args.query if args.all else f"{args.query} has:attachment"
    serve(sqlite_checkpointer(), client, args.port, query, args.max,
          store=ProcessedStore(settings.state_db), run_client=run_client,
          attachments_only=not args.all)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="gmail-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth").set_defaults(fn=cmd_auth)

    f = sub.add_parser("fetch"); f.add_argument("message_id"); f.set_defaults(fn=cmd_fetch)

    d = sub.add_parser("dump"); d.add_argument("message_id"); d.add_argument("path")
    d.set_defaults(fn=cmd_dump)

    pr = sub.add_parser("process"); pr.add_argument("message_id"); pr.set_defaults(fn=cmd_process)

    po = sub.add_parser("poll")
    po.add_argument("--query", default="is:unread in:inbox")
    po.add_argument("--max", type=int, default=5)
    po.add_argument("--loop", action="store_true")
    po.add_argument("--interval", type=int, default=120)
    po.set_defaults(fn=cmd_poll)

    ui = sub.add_parser("ui"); ui.add_argument("--port", type=int, default=8000)
    ui.add_argument("--query", default="in:inbox")
    ui.add_argument("--max", type=int, default=25)
    ui.add_argument("--all", action="store_true", help="also list emails without attachments")
    ui.set_defaults(fn=cmd_ui)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
