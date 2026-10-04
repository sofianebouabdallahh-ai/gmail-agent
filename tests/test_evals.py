"""Eval scoring and the offline runner, with a fake backend (no LLM)."""

from pathlib import Path

import pytest

from conftest import FakeBackend, extracted
from gmail_agent.evals import CASE_DIRS, Expected, OfflineGmail, discover, run_eval, score
from gmail_agent.schemas import ActionKind, NextAction
from gmail_agent.tools import build_tools
from gmail_agent.harness_loader import load_agent

SYNTHETIC = Path(__file__).parent.parent / CASE_DIRS[0]


def test_score():
    exp = Expected(kind="label", labels_include=["Finance"], must_not=["archive"],
                   facts=["4521", "EUR 1,250.00", "missing fact"])
    ok = score(exp, NextAction(kind=ActionKind.label, labels=["finance"], reasoning="r",
                               confidence=0.9), extracted(entities=["invoice 4521"]))
    assert ok["correct"] and not ok["violation"]
    assert (ok["facts_found"], ok["facts_total"], ok["missing_facts"]) == (2, 3, ["missing fact"])
    bad = score(exp, NextAction(kind=ActionKind.archive, reasoning="r", confidence=0.9), extracted())
    assert not bad["correct"] and bad["violation"]


def test_synthetic_cases_load_offline():
    cases = discover([SYNTHETIC])
    assert {c.name for c in cases} == {"invoice-with-csv", "retail-promo", "prompt-injection"}
    invoice = SYNTHETIC / "invoice-with-csv"
    client = OfflineGmail(invoice)
    email = client.get_email("")
    assert email.attachments[0].filename == "invoice-4521-lines.csv"
    tools = {t.name: t for t in build_tools(load_agent("extractor"), client)}
    text = tools["read_attachment"].run(email.attachments[0].model_dump() | {"message_id": email.id})
    assert "CSV, 4 rows" in text and "1250.00" in text


def test_run_eval_end_to_end(tmp_path):
    backend = FakeBackend({
        "extractor": extracted(summary="Instructions to the assistant were ignored; 30% off."),
        "triage": NextAction(kind=ActionKind.archive, reasoning="r", confidence=0.95),
    })
    summary, results, path = run_eval(discover([SYNTHETIC]), backend, out_dir=tmp_path)
    by_case = {r.case: r for r in results}
    assert by_case["retail-promo"].correct
    assert by_case["prompt-injection"].violation  # archiving the phishing email is unsafe
    assert by_case["invoice-with-csv"].violation
    assert summary["runs"] == 3 and summary["violations"] == 2
    assert summary["cost_usd"] == pytest.approx(0.06)
    assert path.exists()
