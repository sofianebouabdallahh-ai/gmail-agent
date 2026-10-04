import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_message() -> dict:
    return json.loads((FIXTURES / "sample_message.json").read_text())
