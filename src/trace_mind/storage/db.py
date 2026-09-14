"""SQLite schema and connection helper.

Deviates from the build plan's draft schema (section 9) in one place, on
purpose: `normalized_events` is keyed `UNIQUE(session_id, byte_start,
byte_end)` rather than `UNIQUE(session_id, content_hash)`. Two identical
short messages ("yes" sent twice) in one session would collide on content
hash alone and the second would silently vanish -- exactly the kind of
missed-event bug Milestone 3 is supposed to catch. Byte range is unique by
construction (it's where the bytes physically sit), so it dedups re-reads
without dedup-ing legitimate repeats. `content_hash` is kept as a column for
integrity checks, not as the identity key.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  root_path TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  external_session_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  transcript_path TEXT,
  cwd TEXT,
  started_at TEXT,
  ended_at TEXT,
  UNIQUE(provider, external_session_id)
);

CREATE TABLE IF NOT EXISTS transcript_cursors (
  session_id TEXT PRIMARY KEY,
  transcript_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL DEFAULT 0,
  file_size INTEGER NOT NULL DEFAULT 0,
  leading_hash TEXT,
  parser_version TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS normalized_events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  turn_id TEXT,
  event_index INTEGER NOT NULL,
  role TEXT,
  event_type TEXT NOT NULL,
  text TEXT,
  tool_name TEXT,
  timestamp TEXT,
  transcript_path TEXT NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  UNIQUE(session_id, byte_start, byte_end, event_index)
);

CREATE INDEX IF NOT EXISTS idx_normalized_events_session
  ON normalized_events(session_id, byte_start, event_index);

CREATE TABLE IF NOT EXISTS hook_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  event_name TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  processed_at TEXT
);

CREATE TABLE IF NOT EXISTS extraction_runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  from_event_index INTEGER,
  to_event_index INTEGER,
  model TEXT,
  prompt_version TEXT,
  input_hash TEXT NOT NULL,
  output_json TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(input_hash, prompt_version)
);

CREATE TABLE IF NOT EXISTS graph_nodes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT,
  status TEXT NOT NULL,
  confidence REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_edges (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  type TEXT NOT NULL,
  reason TEXT,
  confidence REAL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_node_id) REFERENCES graph_nodes(id),
  FOREIGN KEY(target_node_id) REFERENCES graph_nodes(id)
);

CREATE TABLE IF NOT EXISTS provenance (
  id TEXT PRIMARY KEY,
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  normalized_event_id TEXT,
  turn_id TEXT,
  byte_start INTEGER,
  byte_end INTEGER,
  excerpt_hash TEXT,
  extractor_version TEXT,
  created_at TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
