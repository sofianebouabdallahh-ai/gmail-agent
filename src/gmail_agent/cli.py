"""Command line entry point.

  gmail-agent auth                         # run the OAuth flow once, writes token.json
  gmail-agent check                        # validate harness/ agents + skills, print prompts
  gmail-agent fetch <message_id>           # parse one email, no LLM
  gmail-agent dump <message_id> <file>     # save raw JSON as a test fixture
  gmail-agent dump <message_id> --case N   # save an eval case (message + attachments)
  gmail-agent process <message_id> [--dry-run]
  gmail-agent poll [--query Q] [--max N] [--loop --interval S] [--dry-run] [--retry-failed]
  gmail-agent eval [--backend B] [--case NAME] [--repeat N] [--min-accuracy X]
  gmail-agent ui [--port P] [--query Q] [--max N] [--all] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from gmail_agent import logs
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


def cmd_check(args: argparse.Namespace) -> None:
    from gmail_agent.harness_loader import HarnessError, load_all

    try:
        specs = load_all()
    except HarnessError as exc:
        sys.exit(f"harness invalid: {exc}")
    for spec in specs:
        print(f"agent {spec.name}: model={spec.model} effort={spec.effort} "
              f"tools={list(spec.tools)} skills={[s.name for s in spec.skills]} "
              f"output={spec.output.__name__}")
        if args.prompts:
            print(f"\n{'-' * 30} system prompt: {spec.name} {'-' * 30}\n{spec.system_prompt}\n")
    print("harness OK")


def cmd_fetch(args: argparse.Namespace) -> None:
    email = _client().get_email(args.message_id)
    print(email.as_prompt_text())


def cmd_dump(args: argparse.Namespace) -> None:
    client = _client()
    if args.case:
        from gmail_agent.evals import EVALS_DIR, save_case

        path = save_case(client, args.message_id, EVALS_DIR / "cases" / args.case)
        print(f"wrote eval case {path}; now edit {path / 'expected.yaml'}")
        return
    if not args.path:
        sys.exit("give an output file, or --case NAME")
    Path(args.path).write_text(json.dumps(client.get_raw_message(args.message_id), indent=2))
    print(f"wrote {args.path}")


def _answer(payload: dict):
    """Ask the human in the terminal; return the value to resume the graph with."""
    if payload.get("type") == "approve_draft":
        print("\n--- Proposed draft ---")
        print(f"To: {payload['to']}\nSubject: {payload['subject']}\n\n{payload['body']}")
        print(f"\nWhy: {payload['reasoning']}")
        ans = input("Save this draft to Gmail? [y/N] ").strip().lower()
        return {"approved": ans == "y"}
    print("\n--- Needs your attention ---")
    print(json.dumps(payload, indent=2))
    return input("Note for the record (enter to skip): ").strip() or "acknowledged"


def _process_one(graph, store: ProcessedStore, message_id: str, dry_run: bool) -> None:
    from gmail_agent.graph import run_message

    result = run_message(graph, store, message_id, dry_run=dry_run,
                         backend_name=settings.llm_backend)
    while result.status == "waiting" and result.interrupt is not None:
        result = run_message(graph, store, message_id, resume=_answer(result.interrupt),
                             backend_name=settings.llm_backend)
    values = result.values
    email, action = values.get("email"), values.get("action")
    print(f"\n[{message_id}] {email.subject if email else ''}")
    if result.status == "failed":
        print(f"  FAILED: {result.error}")
        return
    if action:
        print(f"  action={action.kind.value} priority={action.priority.value} "
              f"confidence={action.confidence:.2f}")
        print(f"  reasoning: {action.reasoning}")
    for note in values.get("policy_notes") or []:
        print(f"  policy: {note}")
    print(f"  outcome: {values.get('outcome')}")


def _graph(client):
    from gmail_agent.graph import build_graph, sqlite_checkpointer

    return build_graph(client, checkpointer=sqlite_checkpointer())


def cmd_process(args: argparse.Namespace) -> None:
    client = _client()
    _process_one(_graph(client), ProcessedStore(settings.state_db), args.message_id,
                 args.dry_run or settings.dry_run)


def cmd_poll(args: argparse.Namespace) -> None:
    client = _client()
    graph, store = _graph(client), ProcessedStore(settings.state_db)
    dry_run = args.dry_run or settings.dry_run
    while True:
        ids = [m for m in client.list_message_ids(args.query, args.max)
               if not store.seen(m, retry_failed=args.retry_failed)]
        logs.event("poll", new_messages=len(ids), dry_run=dry_run or None)
        for mid in ids:
            _process_one(graph, store, mid, dry_run)  # failures are recorded, not raised
        if not args.loop:
            return
        time.sleep(args.interval)


def cmd_eval(args: argparse.Namespace) -> None:
    from gmail_agent.backends import get_backend
    from gmail_agent.evals import discover, run_eval

    cases = discover(name_filter=args.case)
    if not cases:
        sys.exit("no eval cases found in evals/cases-synthetic or evals/cases")
    backend = get_backend(args.backend or settings.llm_backend)
    print(f"Running {len(cases)} case(s) x{args.repeat} on backend '{backend.name}' "
          f"(min confidence {settings.min_confidence})\n")
    summary, _, path = run_eval(cases, backend, repeat=args.repeat)
    print(f"\n{json.dumps(summary, indent=2)}\nreport: {path}")
    if summary["violations"] or summary["errors"] or summary["accuracy"] < args.min_accuracy:
        sys.exit(1)


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
          attachments_only=not args.all, dry_run=args.dry_run or settings.dry_run)


def main(argv: list[str] | None = None) -> None:
    logs.setup()
    p = argparse.ArgumentParser(prog="gmail-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth").set_defaults(fn=cmd_auth)

    c = sub.add_parser("check", help="validate agent and skill files")
    c.add_argument("--prompts", action="store_true", help="also print the rendered system prompts")
    c.set_defaults(fn=cmd_check)

    f = sub.add_parser("fetch"); f.add_argument("message_id"); f.set_defaults(fn=cmd_fetch)

    d = sub.add_parser("dump"); d.add_argument("message_id"); d.add_argument("path", nargs="?")
    d.add_argument("--case", help="save as eval case evals/cases/<CASE>")
    d.set_defaults(fn=cmd_dump)

    pr = sub.add_parser("process"); pr.add_argument("message_id")
    pr.add_argument("--dry-run", action="store_true", help="decide, but do not change Gmail")
    pr.set_defaults(fn=cmd_process)

    po = sub.add_parser("poll")
    po.add_argument("--query", default="is:unread in:inbox")
    po.add_argument("--max", type=int, default=5)
    po.add_argument("--loop", action="store_true")
    po.add_argument("--interval", type=int, default=120)
    po.add_argument("--dry-run", action="store_true", help="decide, but do not change Gmail")
    po.add_argument("--retry-failed", action="store_true", help="re-run messages that failed before")
    po.set_defaults(fn=cmd_poll)

    ev = sub.add_parser("eval", help="score the agents on saved emails (offline)")
    ev.add_argument("--backend", choices=["api", "claude_code"])
    ev.add_argument("--case", help="only cases whose name contains this")
    ev.add_argument("--repeat", type=int, default=1, help="runs per case, to measure flakiness")
    ev.add_argument("--min-accuracy", type=float, default=0.8)
    ev.set_defaults(fn=cmd_eval)

    ui = sub.add_parser("ui"); ui.add_argument("--port", type=int, default=8000)
    ui.add_argument("--query", default="in:inbox")
    ui.add_argument("--max", type=int, default=25)
    ui.add_argument("--all", action="store_true", help="also list emails without attachments")
    ui.add_argument("--dry-run", action="store_true", help="Process decides but does not change Gmail")
    ui.set_defaults(fn=cmd_ui)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
