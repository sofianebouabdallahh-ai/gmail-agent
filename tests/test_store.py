from gmail_agent.store import ProcessedStore


def test_store_roundtrip(tmp_path):
    s = ProcessedStore(tmp_path / "s.db")
    assert not s.seen("a")
    s.mark("a", "label", {"labels": ["X"]})
    assert s.seen("a")
