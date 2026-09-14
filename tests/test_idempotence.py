import shutil
from pathlib import Path

import pytest

from trace_mind.normalize.pipeline import ingest_session
from trace_mind.storage.db import connect
from trace_mind.transcripts.claude import ClaudeTranscriptAdapter
from trace_mind.transcripts.codex import CodexTranscriptAdapter

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURE = FIXTURES / "claude" / "claude_research_memory_followup.jsonl"
CODEX_FIXTURE = FIXTURES / "codex" / "codex_research_memory_decision.jsonl"


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "research.db")
    yield conn
    conn.close()


def _count_events(conn, session_id) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM normalized_events WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row["n"]


def test_reingest_same_file_is_a_noop(db):
    adapter = CodexTranscriptAdapter()
    session_id = "01a0aaaa-0000-7000-8000-000000000001"

    result1 = ingest_session(db, adapter, session_id, CODEX_FIXTURE)
    assert result1.new_events > 0
    count_after_first = _count_events(db, session_id)

    result2 = ingest_session(db, adapter, session_id, CODEX_FIXTURE)
    assert result2.new_events == 0
    assert _count_events(db, session_id) == count_after_first


def test_incremental_ingest_matches_single_pass(db, tmp_path):
    adapter = ClaudeTranscriptAdapter()
    session_id = "b1c2d3e4-0001-4a2b-9c3d-000000000002"

    full_bytes = CLAUDE_FIXTURE.read_bytes()
    lines = full_bytes.splitlines(keepends=True)
    half = len(lines) // 2

    growing = tmp_path / "growing.jsonl"
    growing.write_bytes(b"".join(lines[:half]))
    ingest_session(db, adapter, session_id, growing)
    mid_count = _count_events(db, session_id)
    assert mid_count > 0

    growing.write_bytes(full_bytes)  # simulate the session appending more turns
    ingest_session(db, adapter, session_id, growing)
    final_count = _count_events(db, session_id)

    full_db_path = tmp_path / "full.db"
    from trace_mind.storage.db import connect as connect2

    full_conn = connect2(full_db_path)
    ingest_session(full_conn, adapter, session_id, CLAUDE_FIXTURE)
    reference_count = _count_events(full_conn, session_id)
    full_conn.close()

    assert final_count == reference_count
    assert final_count > mid_count


def test_simulated_crash_between_read_and_cursor_advance_does_not_skip_events(db, tmp_path):
    """The insert-events + advance-cursor step happens in one transaction.

    If a crash happens mid-ingest, sqlite rolls the whole transaction back,
    so the cursor stays at its old value and the next run re-reads (and
    re-ingests) everything it would have ingested -- nothing is skipped.
    We simulate the crash by aborting the transaction ourselves and then
    re-running ingest_session normally.
    """
    adapter = CodexTranscriptAdapter()
    session_id = "01a0aaaa-0000-7000-8000-000000000001"

    # Manually open a transaction, insert one row, then roll back -- standing
    # in for a process crash after partial work but before commit.
    db.execute("BEGIN")
    db.execute(
        "INSERT INTO normalized_events (id, session_id, provider, turn_id, event_index, role, "
        "event_type, text, tool_name, timestamp, transcript_path, byte_start, byte_end, content_hash) "
        "VALUES ('crash-test', ?, 'codex', NULL, 0, NULL, 'other', NULL, NULL, NULL, 'x', 0, 1, 'h')",
        (session_id,),
    )
    db.execute("ROLLBACK")
    assert _count_events(db, session_id) == 0

    result = ingest_session(db, adapter, session_id, CODEX_FIXTURE)
    assert result.new_events > 0

    cursor_row = db.execute(
        "SELECT byte_offset FROM transcript_cursors WHERE session_id = ?", (session_id,)
    ).fetchone()
    assert cursor_row["byte_offset"] == CODEX_FIXTURE.stat().st_size


def test_second_file_for_same_session_id_does_not_delete_first_files_events(db, tmp_path):
    """A Codex compaction rewrites a session's rollout under a NEW filename
    but keeps the SAME session_id. Ingesting that second, unrelated file
    must not be mistaken for truncation of the first and must not delete
    the first file's already-ingested events -- this happened for real
    against an actual multi-file Codex session and lost 17.8k events.
    """
    adapter = CodexTranscriptAdapter()
    session_id = "01a0aaaa-0000-7000-8000-000000000001"

    first_file = tmp_path / "rollout-a.jsonl"
    first_file.write_bytes(CODEX_FIXTURE.read_bytes())
    ingest_session(db, adapter, session_id, first_file)
    first_file_count = _count_events(db, session_id)
    assert first_file_count > 0

    # A second, smaller file for the SAME session_id but a DIFFERENT path --
    # must be treated as new content to read from 0, not as the first file
    # having shrunk.
    second_file = tmp_path / "rollout-b-continuation.jsonl"
    second_file.write_bytes(CODEX_FIXTURE.read_bytes()[:900])  # a few complete lines
    result = ingest_session(db, adapter, session_id, second_file)
    assert result.rotated is False

    total_count = _count_events(db, session_id)
    assert total_count > first_file_count, "first file's events must survive ingesting a second file"

    # Ingesting the first file again afterward is still a pure no-op.
    result_again = ingest_session(db, adapter, session_id, first_file)
    assert result_again.new_events == 0
    assert _count_events(db, session_id) == total_count


def test_truncated_transcript_resets_cursor_instead_of_erroring(db, tmp_path):
    adapter = CodexTranscriptAdapter()
    session_id = "01a0aaaa-0000-7000-8000-000000000001"

    live = tmp_path / "live.jsonl"
    shutil.copy(CODEX_FIXTURE, live)
    ingest_session(db, adapter, session_id, live)
    assert _count_events(db, session_id) > 0

    # Truncate to simulate the file being rewritten/rotated underneath us.
    lines = live.read_bytes().splitlines(keepends=True)
    live.write_bytes(b"".join(lines[:2]))

    result = ingest_session(db, adapter, session_id, live)
    assert result.rotated is True

    # A fresh DB ingesting only the truncated content from scratch should
    # see exactly the same event count -- proving the reset actually
    # re-ingested the (now-shorter) file rather than just clearing state.
    reference_conn = connect(tmp_path / "reference.db")
    ingest_session(reference_conn, adapter, session_id, live)
    reference_count = _count_events(reference_conn, session_id)
    reference_conn.close()

    assert _count_events(db, session_id) == reference_count
    assert reference_count > 0
