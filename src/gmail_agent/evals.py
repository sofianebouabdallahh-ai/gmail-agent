"""Offline evals: run the extractor + triage agents (+ policy) on saved emails and score
the decisions against expectations. Nothing touches the real mailbox.

A case is a folder:

    message.json                 raw `users.messages.get(format=full)` response
    attachments/manifest.json    {attachment_id: file name}, plus the files themselves
    expected.yaml                what a correct decision looks like (see `Expected`)

Create one from a real email with `gmail-agent dump <message_id> --case <name>`, then
fill in expected.yaml. Real cases live in evals/cases/ (gitignored: they hold your mail);
synthetic ones in evals/cases-synthetic/ are committed.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

from gmail_agent import policy
from gmail_agent.backends import Backend, run_agent
from gmail_agent.config import settings
from gmail_agent.gmail.client import parse_message
from gmail_agent.graph import triage_message
from gmail_agent.harness_loader import AgentSpec, load_agent
from gmail_agent.schemas import ActionKind, Email, ExtractedEmail, NextAction

EVALS_DIR = Path("evals")
CASE_DIRS = (EVALS_DIR / "cases-synthetic", EVALS_DIR / "cases")


class Expected(BaseModel):
    kind: list[ActionKind]  # any of these final actions is correct
    labels_include: list[str] = []  # when the action is `label`, these must be among the labels
    must_not: list[ActionKind] = []  # unsafe outcomes: any of these fails the whole eval run
    facts: list[str] = []  # strings the extraction must contain (case-insensitive)
    notes: str = ""

    @field_validator("kind", "must_not", mode="before")
    @classmethod
    def _one_or_many(cls, v):
        return [v] if isinstance(v, str) else v


# --------------------------------------------------------------------------- #
# Cases
# --------------------------------------------------------------------------- #
class OfflineGmail:
    """Serves one saved case through the GmailClient methods the agents' tools use."""

    def __init__(self, case_dir: Path):
        self.raw = json.loads((case_dir / "message.json").read_text())
        manifest = case_dir / "attachments" / "manifest.json"
        self.files = json.loads(manifest.read_text()) if manifest.is_file() else {}
        self.dir = case_dir / "attachments"

    def get_email(self, message_id: str) -> Email:
        return parse_message(self.raw)

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        if attachment_id not in self.files:
            raise KeyError(f"attachment {attachment_id!r} is not part of this case")
        return (self.dir / self.files[attachment_id]).read_bytes()

    def search(self, query: str, max_results: int = 5) -> list[Email]:
        return []  # offline: no mailbox history

    def get_thread(self, thread_id: str) -> list[Email]:
        email = parse_message(self.raw)
        return [email] if thread_id == email.thread_id else []


def save_case(client, message_id: str, case_dir: Path) -> Path:
    """Snapshot a real email (and its attachments) into an eval case folder."""
    raw = client.get_raw_message(message_id)
    email = parse_message(raw)
    att_dir = case_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "message.json").write_text(json.dumps(raw, indent=2))
    manifest = {}
    for i, a in enumerate(email.attachments):
        name = f"{i}-{re.sub(r'[^A-Za-z0-9._-]+', '_', a.filename)[-80:]}"
        (att_dir / name).write_bytes(client.download_attachment(message_id, a.attachment_id))
        manifest[a.attachment_id] = name
    (att_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    expected = case_dir / "expected.yaml"
    if not expected.exists():
        expected.write_text(
            f"# {email.subject}\n# From: {email.sender}\n"
            "kind: [label]          # correct final action(s): draft_reply label archive needs_human ignore\n"
            "labels_include: []     # e.g. [Travel]\n"
            "must_not: [archive]    # outcomes that would be harmful\n"
            "facts: []              # strings the extraction must contain, e.g. booking reference\n"
        )
    return case_dir


def discover(dirs=CASE_DIRS, name_filter: str | None = None) -> list[Path]:
    cases = [p for d in dirs if d.is_dir() for p in sorted(d.iterdir())
             if (p / "message.json").is_file() and (p / "expected.yaml").is_file()]
    return [c for c in cases if not name_filter or name_filter in c.name]


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
@dataclass
class CaseResult:
    case: str
    expected: list[str]
    proposed: str | None = None
    action: str | None = None
    correct: bool = False
    violation: bool = False
    facts_found: int = 0
    facts_total: int = 0
    missing_facts: list[str] = field(default_factory=list)
    policy_notes: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    duration_ms: int = 0
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None


def score(expected: Expected, action: NextAction, extracted: ExtractedEmail) -> dict:
    labels_ok = (action.kind != ActionKind.label or
                 {l.lower() for l in expected.labels_include} <= {l.lower() for l in action.labels})
    haystack = " ".join(extracted.model_dump_json().lower().split())
    missing = [f for f in expected.facts if " ".join(f.lower().split()) not in haystack]
    return {
        "correct": action.kind in expected.kind and labels_ok,
        "violation": action.kind in expected.must_not,
        "facts_found": len(expected.facts) - len(missing),
        "facts_total": len(expected.facts),
        "missing_facts": missing,
    }


def run_case(case_dir: Path, backend: Backend, extractor: AgentSpec,
             triage: AgentSpec) -> CaseResult:
    expected = Expected.model_validate(yaml.safe_load((case_dir / "expected.yaml").read_text()))
    result = CaseResult(case=case_dir.name, expected=[k.value for k in expected.kind])
    try:
        client = OfflineGmail(case_dir)
        email = client.get_email("")
        extracted, u1 = run_agent(backend, extractor, client, email.as_prompt_text())
        proposed, u2 = run_agent(backend, triage, client, triage_message(email, extracted))
        action, notes = policy.enforce(proposed, min_confidence=settings.min_confidence,
                                       allowed_labels=settings.allowed_labels)
    except Exception as exc:
        result.error = f"{exc.__class__.__name__}: {exc}"[:500]
        return result
    costs = [u.cost_usd for u in (u1, u2) if u.cost_usd is not None]
    result.proposed, result.action, result.policy_notes = proposed.kind.value, action.kind.value, notes
    result.cost_usd = round(sum(costs), 6) if costs else None
    result.duration_ms = u1.duration_ms + u2.duration_ms
    result.tool_calls = [f"extractor:{t}" for t in u1.tool_calls] + [f"triage:{t}" for t in u2.tool_calls]
    for k, v in score(expected, action, extracted).items():
        setattr(result, k, v)
    return result


def summarize(results: list[CaseResult]) -> dict:
    n = len(results) or 1
    facts_total = sum(r.facts_total for r in results)
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    return {
        "runs": len(results),
        "accuracy": round(sum(r.correct for r in results) / n, 3),
        "violations": sum(r.violation for r in results),
        "errors": sum(r.error is not None for r in results),
        "fact_recall": round(sum(r.facts_found for r in results) / facts_total, 3) if facts_total else None,
        "cost_usd": round(sum(costs), 4) if costs else None,
        "avg_duration_s": round(sum(r.duration_ms for r in results) / n / 1000, 1),
    }


def run_eval(cases: list[Path], backend: Backend, repeat: int = 1,
             out_dir: Path = EVALS_DIR / "results") -> tuple[dict, list[CaseResult], Path]:
    extractor, triage = load_agent("extractor"), load_agent("triage")
    results = []
    for case in cases:
        for _ in range(repeat):
            r = run_case(case, backend, extractor, triage)
            results.append(r)
            print(_row(r), flush=True)
    summary = summarize(results)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{backend.name}.json"
    path.write_text(json.dumps({"summary": summary, "backend": backend.name,
                                "results": [asdict(r) for r in results]}, indent=2))
    return summary, results, path


def _row(r: CaseResult) -> str:
    if r.error:
        return f"  ERROR  {r.case:<32} {r.error}"
    mark = "VIOLATION" if r.violation else ("ok" if r.correct else "WRONG")
    arrow = r.action if r.proposed == r.action else f"{r.proposed}->{r.action}"
    facts = f"facts {r.facts_found}/{r.facts_total}" if r.facts_total else ""
    cost = f"${r.cost_usd:.3f}" if r.cost_usd is not None else ""
    return (f"  {mark:<9} {r.case:<32} got {arrow:<26} want {'|'.join(r.expected):<20} "
            f"{facts:<10} {r.duration_ms / 1000:>5.1f}s {cost}")
