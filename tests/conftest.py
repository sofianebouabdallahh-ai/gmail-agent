import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

from gmail_agent.schemas import ExtractedEmail, RunUsage  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_message() -> dict:
    return json.loads((FIXTURES / "sample_message.json").read_text())


class FakeBackend:
    """Returns preset outputs per agent name; records what it was asked."""

    name = "fake"

    def __init__(self, outputs: dict):
        self.outputs = outputs
        self.calls: list[tuple[str, str, list[str]]] = []

    def run(self, spec, user_message, tools):
        self.calls.append((spec.name, user_message, [t.name for t in tools]))
        out = self.outputs[spec.name]
        if isinstance(out, Exception):
            raise out
        return out, RunUsage(agent=spec.name, backend=self.name, model=spec.model,
                             input_tokens=100, output_tokens=20, cost_usd=0.01)


def extracted(**kw) -> ExtractedEmail:
    base = dict(sender_name="Acme Billing", sender_email="billing@acme.example",
                subject="Invoice #4521", summary="Invoice for September, EUR 1,250.00.",
                intent="invoice")
    return ExtractedEmail(**{**base, **kw})
