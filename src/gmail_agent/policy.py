"""Guardrails enforced in code after the triage agent decides, whatever the model said.

The prompts ask for the same behaviour, but a prompt is a request; this is a guarantee.
Every override is explained in the returned notes, which are stored and shown in the UI.
"""

from __future__ import annotations

from collections.abc import Iterable

from gmail_agent.schemas import ActionKind, NextAction


def _to_human(action: NextAction, why: str) -> NextAction:
    return action.model_copy(update={
        "kind": ActionKind.needs_human,
        "reasoning": f"{action.reasoning} [policy: {why}]",
    })


def enforce(action: NextAction, *, min_confidence: float,
            allowed_labels: Iterable[str]) -> tuple[NextAction, list[str]]:
    notes: list[str] = []
    proposed = action.kind.value

    if action.kind != ActionKind.needs_human and action.confidence < min_confidence:
        notes.append(f"confidence {action.confidence:.2f} < {min_confidence:.2f}: "
                     f"{proposed} sent to a human instead")
        return _to_human(action, "low confidence"), notes

    if action.kind == ActionKind.draft_reply and not (action.draft_reply or "").strip():
        notes.append("draft_reply had an empty body: sent to a human instead")
        return _to_human(action, "empty draft"), notes

    if action.kind == ActionKind.label:
        canonical = {label.lower(): label for label in allowed_labels}
        kept, dropped = [], []
        for label in action.labels:
            match = canonical.get(label.strip().lower())
            (kept if match else dropped).append(match or label)
        kept = list(dict.fromkeys(kept))  # de-duplicate, keep order
        if dropped:
            notes.append(f"labels not in the allowlist dropped: {dropped}")
        if not kept:
            notes.append("no allowed label left: sent to a human instead")
            return _to_human(action, "no allowed label"), notes
        action = action.model_copy(update={"labels": kept})

    if action.kind == ActionKind.schedule:
        due = f" (due {action.due_by})" if action.due_by else ""
        notes.append(f"schedule is not automated yet{due}: sent to a human instead")
        return _to_human(action, f"follow-up needed{due}"), notes

    return action, notes
