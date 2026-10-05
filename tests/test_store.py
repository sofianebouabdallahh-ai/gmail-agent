import sqlite3

from gmail_agent.store import ProcessedStore


def test_store_roundtrip(tmp_path):
    s = ProcessedStore(tmp_path / "s.db")
    assert not s.seen("a") and s.status("a") is None
    s.mark("a", action_kind="label", action={"labels": ["X"]}, cost_usd=0.02)
    assert s.seen("a") and s.status("a") == "done"
    assert s.all()["a"]["cost_usd"] == 0.02


def test_failed_messages_can_be_retried(tmp_path):
    s = ProcessedStore(tmp_path / "s.db")
    s.mark("f", status="failed", error="boom")
    assert s.seen("f") and not s.seen("f", retry_failed=True)
    assert s.all()["f"]["error"] == "boom"


def test_dry_runs_do_not_block_a_real_run(tmp_path):
    s = ProcessedStore(tmp_path / "s.db")
    s.mark("d", status="dry_run", action_kind="archive")
    assert not s.seen("d")


def test_migrates_first_version_schema(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE processed (message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL,"
                 " action_kind TEXT, action_json TEXT)")
    conn.execute("INSERT INTO processed VALUES ('old', '2026-10-04', 'archive', NULL)")
    conn.commit()
    conn.close()
    s = ProcessedStore(path)
    assert s.status("old") == "done"  # existing rows count as done
    s.mark("new", status="waiting")
    assert s.status("new") == "waiting"
