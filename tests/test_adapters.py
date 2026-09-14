from pathlib import Path

from trace_mind.transcripts.claude import ClaudeTranscriptAdapter
from trace_mind.transcripts.codex import CodexTranscriptAdapter

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURE = FIXTURES / "claude" / "claude_research_memory_followup.jsonl"
CODEX_FIXTURE = FIXTURES / "codex" / "codex_research_memory_decision.jsonl"


def test_claude_detect():
    adapter = ClaudeTranscriptAdapter()
    assert adapter.detect(CLAUDE_FIXTURE)
    assert not adapter.detect(CODEX_FIXTURE)


def test_codex_detect():
    adapter = CodexTranscriptAdapter()
    assert adapter.detect(CODEX_FIXTURE)
    assert not adapter.detect(CLAUDE_FIXTURE)


def test_claude_read_delta_full_file():
    adapter = ClaudeTranscriptAdapter()
    delta = adapter.read_delta(CLAUDE_FIXTURE, "b1c2d3e4-0001-4a2b-9c3d-000000000002", 0)

    assert delta.next_byte_offset == CLAUDE_FIXTURE.stat().st_size
    assert len(delta.events) > 0
    assert all(e.provider == "claude" for e in delta.events)

    user_texts = [e.text for e in delta.events if e.event_type == "user_message"]
    assert any("temporal evolution" in t for t in user_texts if t)

    tool_calls = [e for e in delta.events if e.event_type == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_name == "Write"

    tool_results = [e for e in delta.events if e.event_type == "tool_result"]
    assert len(tool_results) == 1


def test_codex_read_delta_full_file():
    adapter = CodexTranscriptAdapter()
    delta = adapter.read_delta(CODEX_FIXTURE, "01a0aaaa-0000-7000-8000-000000000001", 0)

    assert delta.next_byte_offset == CODEX_FIXTURE.stat().st_size
    assert len(delta.events) > 0
    assert all(e.provider == "codex" for e in delta.events)

    user_msgs = [e.text for e in delta.events if e.event_type == "user_message"]
    assert any("Graphiti" in t for t in user_msgs if t)

    assistant_msgs = [e.text for e in delta.events if e.event_type == "assistant_message"]
    assert any("dormant option" in t for t in assistant_msgs if t)

    compactions = [e for e in delta.events if e.event_type == "compaction"]
    assert len(compactions) == 1


def test_claude_incremental_read_matches_full_read():
    """A read that resumes mid-file must reproduce identical content_hash
    values for the events at-or-after the resume point -- proving identity
    is a function of byte position, not of where the read call started.
    (Filtering a from-zero read wouldn't exercise this: it would pass
    trivially regardless of how identity is computed.)
    """
    adapter = ClaudeTranscriptAdapter()
    session_id = "b1c2d3e4-0001-4a2b-9c3d-000000000002"

    full = adapter.read_delta(CLAUDE_FIXTURE, session_id, 0)
    boundary = full.events[1].byte_end

    resumed = adapter.read_delta(CLAUDE_FIXTURE, session_id, boundary)

    expected_hashes = {e.content_hash for e in full.events if e.byte_start >= boundary}
    resumed_hashes = {e.content_hash for e in resumed.events}
    assert resumed_hashes == expected_hashes


def test_incomplete_trailing_line_is_not_consumed(tmp_path):
    adapter = ClaudeTranscriptAdapter()
    session_id = "test-session"
    partial = tmp_path / "partial.jsonl"
    complete_line = (
        '{"parentUuid":null,"isSidechain":false,"promptId":"p1","type":"user",'
        '"message":{"role":"user","content":"hello"},"uuid":"u1",'
        '"timestamp":"2026-01-01T00:00:00.000Z","userType":"external","cwd":"/x",'
        '"sessionId":"test-session","version":"1.0","gitBranch":"main"}\n'
    )
    truncated_line = '{"type":"user","message":{"role":"user","content":"cut off'  # no trailing newline
    partial.write_text(complete_line + truncated_line)

    delta = adapter.read_delta(partial, session_id, 0)

    assert len(delta.events) == 1
    assert delta.next_byte_offset == len(complete_line.encode("utf-8"))
