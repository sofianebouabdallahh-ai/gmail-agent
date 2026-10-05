from gmail_agent.policy import enforce
from gmail_agent.schemas import ActionKind, NextAction

ALLOWED = ("Finance", "Travel", "Receipts")


def _enforce(**kw):
    return enforce(NextAction(**{"reasoning": "r", "confidence": 0.9, **kw}),
                   min_confidence=0.6, allowed_labels=ALLOWED)


def test_confident_action_passes_unchanged():
    action, notes = _enforce(kind=ActionKind.archive)
    assert action.kind == ActionKind.archive and notes == []


def test_low_confidence_goes_to_human():
    action, notes = _enforce(kind=ActionKind.archive, confidence=0.59)
    assert action.kind == ActionKind.needs_human
    assert "0.59 < 0.60" in notes[0] and "[policy: low confidence]" in action.reasoning


def test_needs_human_is_never_overridden_for_confidence():
    action, notes = _enforce(kind=ActionKind.needs_human, confidence=0.1)
    assert action.kind == ActionKind.needs_human and notes == []


def test_empty_draft_goes_to_human():
    action, _ = _enforce(kind=ActionKind.draft_reply, draft_reply="   ")
    assert action.kind == ActionKind.needs_human


def test_labels_are_normalised_and_filtered():
    action, notes = _enforce(kind=ActionKind.label, labels=["travel", "Crypto", "Travel"])
    assert action.kind == ActionKind.label and action.labels == ["Travel"]
    assert "Crypto" in notes[0]


def test_no_allowed_label_goes_to_human():
    action, notes = _enforce(kind=ActionKind.label, labels=["Crypto"])
    assert action.kind == ActionKind.needs_human and len(notes) == 2


def test_schedule_goes_to_human_with_due_date():
    action, notes = _enforce(kind=ActionKind.schedule, due_by="2026-10-20")
    assert action.kind == ActionKind.needs_human and "2026-10-20" in notes[0]
