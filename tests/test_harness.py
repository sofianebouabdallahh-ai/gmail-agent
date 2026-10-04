"""Agent/skill loading, validation, prompt rendering and the skill tools."""

from dataclasses import replace
from pathlib import Path

import pytest

from gmail_agent.config import settings
from gmail_agent.harness_loader import HarnessError, load_agent, load_all
from gmail_agent.schemas import ExtractedEmail, NextAction
from gmail_agent.tools import build_tools

NO_OWNER = Path("/nonexistent/owner.md")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def harness(tmp_path):
    _write(tmp_path, "agents/a.md", "---\nname: a\ndescription: test agent\nmodel: triage\n"
           "tools: [search_gmail]\nskills: [s1]\noutput: NextAction\n---\nYou are A.\n")
    _write(tmp_path, "skills/s1/SKILL.md", "---\nname: s1\ndescription: does s1\n---\nS1 BODY\n")
    _write(tmp_path, "skills/s1/references/r.md", "REF")
    return tmp_path


def test_shipped_harness_is_valid():
    specs = {s.name: s for s in load_all()}
    assert set(specs) == {"extractor", "triage"}
    assert specs["extractor"].output is ExtractedEmail
    assert specs["triage"].output is NextAction
    assert [s.name for s in specs["triage"].skills] == ["triage-policy", "reply-drafting", "labeling"]


def test_prompt_lists_skills_but_not_their_bodies(harness):
    spec = load_agent("a", harness, owner_file=NO_OWNER)
    prompt = spec.system_prompt
    assert prompt.startswith("You are A.")
    assert "`s1`: does s1" in prompt and "S1 BODY" not in prompt
    assert "Owner profile" not in prompt


def test_owner_profile_is_appended(harness, tmp_path):
    owner = tmp_path / "owner.md"
    owner.write_text("Name: Sam")
    assert "Name: Sam" in load_agent("a", harness, owner_file=owner).system_prompt


def test_skill_tools(harness):
    trace: list[str] = []
    tools = {t.name: t for t in build_tools(load_agent("a", harness, owner_file=NO_OWNER),
                                            client=None, trace=trace)}
    assert set(tools) == {"search_gmail", "load_skill", "read_skill_file"}
    assert tools["load_skill"].run({"name": "s1"}) == "S1 BODY"
    assert tools["read_skill_file"].run({"skill": "s1", "path": "references/r.md"}) == "REF"
    # mistakes come back to the model as text, not exceptions
    assert tools["load_skill"].run({"name": "other"}).startswith("Error:")
    assert tools["read_skill_file"].run({"skill": "s1", "path": "../../agents/a.md"}).startswith("Error:")
    assert tools["read_skill_file"].run({"skill": "s1", "path": "/etc/passwd"}).startswith("Error:")
    assert trace[:2] == ["load_skill(s1)", "read_skill_file(s1)"]


@pytest.mark.parametrize("agent_md, message", [
    ("---\nname: b\ndescription: x\nmodel: triage\noutput: NextAction\n---\n", "must match"),
    ("---\nname: a\nmodel: triage\noutput: NextAction\n---\n", "'description'"),
    ("---\nname: a\ndescription: x\nmodel: gpt\noutput: NextAction\n---\n", "model must be"),
    ("---\nname: a\ndescription: x\nmodel: triage\ntools: [send_email]\noutput: NextAction\n---\n",
     "unknown tools"),
    ("---\nname: a\ndescription: x\nmodel: triage\nskills: [nope]\noutput: NextAction\n---\n",
     "skills/nope/SKILL.md not found"),
    ("---\nname: a\ndescription: x\nmodel: triage\noutput: Dict\n---\n", "output must be"),
    ("no frontmatter", "frontmatter"),
])
def test_invalid_agent_files_fail_fast(harness, agent_md, message):
    _write(harness, "agents/a.md", agent_md)
    with pytest.raises(HarnessError, match=message):
        load_agent("a", harness, owner_file=NO_OWNER)


def test_skill_labels_must_be_allowed(harness):
    _write(harness, "skills/s1/SKILL.md",
           "---\nname: s1\ndescription: d\nlabels: [Finance, Crypto]\n---\nbody")
    with pytest.raises(HarnessError, match="Crypto"):
        load_agent("a", harness, owner_file=NO_OWNER)
    cfg = replace(settings, allowed_labels=("Finance", "Crypto"))
    assert load_agent("a", harness, cfg, owner_file=NO_OWNER)


def test_tool_adapters_produce_valid_schemas():
    from gmail_agent.backends.claude_code import to_sdk
    from gmail_agent.backends.langchain_api import to_langchain

    spec = load_agent("triage", owner_file=NO_OWNER)
    for t in build_tools(spec, client=None):
        lc = to_langchain(t)
        assert lc.name == t.name and lc.args  # args derived from the Pydantic model
        sdk = to_sdk(t)
        assert sdk.name == t.name and sdk.input_schema["type"] == "object"
    load_skill = next(t for t in build_tools(spec, client=None) if t.name == "load_skill")
    assert "Triage policy" in to_langchain(load_skill).invoke({"name": "triage-policy"})
