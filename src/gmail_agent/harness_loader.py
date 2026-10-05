"""Load agent definitions (AGENT.md) and skills (SKILL.md) from the harness folder.

Layout, shipped inside the package so it works when installed:

    harness/agents/<name>.md            frontmatter + always-on system prompt
    harness/skills/<name>/SKILL.md      frontmatter + instructions, loaded on demand
    harness/skills/<name>/references/   optional extra files a skill points to

Skills use progressive disclosure: an agent's prompt lists only each skill's name and
description; the agent calls the `load_skill` tool to read the full instructions when it
needs them. Everything is validated when an agent is loaded, so a typo in a Markdown
file fails at startup instead of mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel

from gmail_agent.config import Settings, settings as default_settings
from gmail_agent.schemas import ExtractedEmail, NextAction

HARNESS: Traversable = files("gmail_agent") / "harness"
OUTPUTS: dict[str, type[BaseModel]] = {"ExtractedEmail": ExtractedEmail, "NextAction": NextAction}


class HarnessError(ValueError):
    """An agent or skill file is missing or invalid."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    root: Traversable
    meta: dict = field(default_factory=dict)

    def read_file(self, rel_path: str) -> str:
        """Read a file inside this skill's folder. Refuses anything outside it."""
        rel = PurePosixPath(rel_path)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise HarnessError(f"invalid path {rel_path!r} for skill {self.name}")
        target = self.root.joinpath(*rel.parts)
        if not target.is_file():
            raise HarnessError(f"skill {self.name} has no file {rel_path!r}")
        return target.read_text(encoding="utf-8")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    model: str
    effort: str
    tools: tuple[str, ...]
    skills: tuple[Skill, ...]
    output: type[BaseModel]
    prompt: str
    owner_profile: str = ""

    def skill(self, name: str) -> Skill:
        for s in self.skills:
            if s.name == name:
                return s
        raise HarnessError(
            f"agent {self.name} has no skill {name!r}; available: {[s.name for s in self.skills]}"
        )

    @property
    def system_prompt(self) -> str:
        parts = [self.prompt.strip()]
        if self.owner_profile:
            parts.append("## Owner profile\n\n" + self.owner_profile.strip())
        if self.skills:
            index = "\n".join(f"- `{s.name}`: {s.description}" for s in self.skills)
            parts.append(
                "## Skills\n\n"
                "Each skill holds instructions you must follow in its area. Call "
                "`load_skill` with its name to read it before acting in that area, and "
                "`read_skill_file` for files a skill points to.\n\n" + index
            )
        return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _frontmatter(text: str, where: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise HarnessError(f"{where}: must start with a '---' YAML frontmatter block")
    try:
        _, raw, body = text.split("---\n", 2)
    except ValueError:
        raise HarnessError(f"{where}: frontmatter block is not closed with '---'") from None
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise HarnessError(f"{where}: invalid YAML frontmatter: {exc}") from None
    if not isinstance(meta, dict):
        raise HarnessError(f"{where}: frontmatter must be a mapping")
    return meta, body


def _require(meta: dict, key: str, where: str) -> str:
    value = meta.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{where}: frontmatter needs a non-empty '{key}'")
    return value.strip()


def load_skill(name: str, root: Traversable = HARNESS,
               cfg: Settings = default_settings) -> Skill:
    folder = root / "skills" / name
    path = folder / "SKILL.md"
    where = f"skills/{name}/SKILL.md"
    if not path.is_file():
        raise HarnessError(f"{where} not found")
    meta, body = _frontmatter(path.read_text(encoding="utf-8"), where)
    if _require(meta, "name", where) != name:
        raise HarnessError(f"{where}: name {meta['name']!r} must match its folder {name!r}")
    labels = meta.get("labels")
    if labels is not None:
        unknown = sorted(set(labels) - set(cfg.allowed_labels))
        if unknown:
            raise HarnessError(
                f"{where}: labels {unknown} are not in ALLOWED_LABELS {list(cfg.allowed_labels)}"
            )
    return Skill(name=name, description=_require(meta, "description", where), body=body.strip(),
                 root=folder, meta=meta)


def load_agent(name: str, root: Traversable = HARNESS, cfg: Settings = default_settings,
               owner_file: Path | None = None) -> AgentSpec:
    from gmail_agent.tools import GMAIL_TOOL_NAMES  # avoid an import cycle

    path = root / "agents" / f"{name}.md"
    where = f"agents/{name}.md"
    if not path.is_file():
        raise HarnessError(f"{where} not found")
    meta, body = _frontmatter(path.read_text(encoding="utf-8"), where)
    if _require(meta, "name", where) != name:
        raise HarnessError(f"{where}: name {meta['name']!r} must match the file name {name!r}")

    models = {"extract": (cfg.extract_model, cfg.extract_effort),
              "triage": (cfg.triage_model, cfg.triage_effort)}
    model_key = _require(meta, "model", where)
    if model_key not in models:
        raise HarnessError(f"{where}: model must be one of {sorted(models)}, got {model_key!r}")

    tools = tuple(meta.get("tools") or ())
    unknown_tools = [t for t in tools if t not in GMAIL_TOOL_NAMES]
    if unknown_tools:
        raise HarnessError(f"{where}: unknown tools {unknown_tools}; known: {list(GMAIL_TOOL_NAMES)}")

    output = _require(meta, "output", where)
    if output not in OUTPUTS:
        raise HarnessError(f"{where}: output must be one of {sorted(OUTPUTS)}, got {output!r}")

    owner_path = owner_file if owner_file is not None else cfg.owner_file
    owner = owner_path.read_text(encoding="utf-8") if owner_path.is_file() else ""
    model, effort = models[model_key]
    return AgentSpec(
        name=name,
        description=_require(meta, "description", where),
        model=model,
        effort=effort,
        tools=tools,
        skills=tuple(load_skill(s, root, cfg) for s in meta.get("skills") or ()),
        output=OUTPUTS[output],
        prompt=body.strip(),
        owner_profile=owner,
    )


def load_all(root: Traversable = HARNESS, cfg: Settings = default_settings) -> list[AgentSpec]:
    """Load and validate every agent (and the skills they use)."""
    names = sorted(p.name[:-3] for p in (root / "agents").iterdir() if p.name.endswith(".md"))
    return [load_agent(n, root, cfg) for n in names]
