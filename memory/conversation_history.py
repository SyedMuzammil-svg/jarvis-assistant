"""
Conversation History Manager with SQLite + FTS5 full-text search.
Stores every turn (user, assistant, tool calls) with millisecond-fast full-text indexing.
"""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock
import sys

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = get_base_dir()
DB_PATH = BASE_DIR / "memory" / "conversations.db"
_lock = Lock()

class ConversationHistory:
    """Persistent, searchable SQLite FTS5 conversation store."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.current_session_id: str | None = None
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with _lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        summary TEXT,
                        turn_count INTEGER DEFAULT 0
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        tool_name TEXT,
                        tool_args TEXT,
                        tool_result TEXT,
                        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                    )
                """)
                # Create FTS5 virtual table for lightning-fast search
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                        content,
                        role,
                        session_id UNINDEXED,
                        content='turns',
                        content_rowid='id'
                    )
                """)
                # Triggers to keep FTS table in sync
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
                        INSERT INTO turns_fts(rowid, content, role, session_id)
                        VALUES (new.id, new.content, new.role, new.session_id);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
                        INSERT INTO turns_fts(turns_fts, rowid, content, role, session_id)
                        VALUES('delete', old.id, old.content, old.role, old.session_id);
                    END;
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
                        INSERT INTO turns_fts(turns_fts, rowid, content, role, session_id)
                        VALUES('delete', old.id, old.content, old.role, old.session_id);
                        INSERT INTO turns_fts(rowid, content, role, session_id)
                        VALUES (new.id, new.content, new.role, new.session_id);
                    END;
                """)
                conn.commit()
            finally:
                conn.close()

    def start_session(self, session_id: str | None = None) -> str:
        """Start a new session record."""
        sid = session_id or f"sess_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = datetime.now().isoformat()
        with _lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (session_id, started_at) VALUES (?, ?)",
                    (sid, now),
                )
                conn.commit()
                self.current_session_id = sid
            finally:
                conn.close()
        return sid

    def add_turn(
        self,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_args: str | None = None,
        tool_result: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Record a single turn (user, assistant, system, tool)."""
        sid = session_id or self.current_session_id
        if not sid:
            sid = self.start_session()

        content = (content or "").strip()
        if not content and not tool_name:
            return -1

        now = datetime.now().isoformat()
        with _lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO turns (session_id, timestamp, role, content, tool_name, tool_args, tool_result)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sid, now, role, content, tool_name, tool_args, tool_result),
                )
                turn_id = cur.lastrowid
                cur.execute(
                    "UPDATE sessions SET turn_count = turn_count + 1 WHERE session_id = ?",
                    (sid,),
                )
                conn.commit()
                return turn_id
            finally:
                conn.close()

    def end_session(self, summary: str | None = None, session_id: str | None = None) -> None:
        """Close out session and record its summary."""
        sid = session_id or self.current_session_id
        if not sid:
            return
        now = datetime.now().isoformat()
        with _lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE sessions SET ended_at = ?, summary = ? WHERE session_id = ?",
                    (now, summary, sid),
                )
                conn.commit()
            finally:
                conn.close()

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Full-text search across past conversations using FTS5."""
        if not query or not query.strip():
            return []

        # Sanitize query for FTS5 syntax
        clean_words = [w for w in query.replace('"', '').replace("'", "").split() if w]
        if not clean_words:
            return []
        match_query = " ".join(f'"{w}"*' for w in clean_words)

        with _lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                # Query turns joined with FTS match
                cur.execute(
                    """
                    SELECT t.id, t.session_id, t.timestamp, t.role, t.content,
                           t.tool_name, t.tool_result,
                           snippet(turns_fts, 0, '<b>', '</b>', '...', 15) as snippet
                    FROM turns_fts f
                    JOIN turns t ON f.rowid = t.id
                    WHERE turns_fts MATCH ?
                    ORDER BY t.id DESC
                    LIMIT ?
                    """,
                    (match_query, limit),
                )
                rows = cur.fetchall()
                results = []
                for r in rows:
                    results.append({
                        "id": r["id"],
                        "session_id": r["session_id"],
                        "timestamp": r["timestamp"],
                        "role": r["role"],
                        "content": r["content"],
                        "tool_name": r["tool_name"],
                        "tool_result": r["tool_result"],
                        "snippet": r["snippet"],
                    })
                return results
            except Exception as e:
                # Fallback to LIKE if FTS expression has syntax errors
                try:
                    like_pat = f"%{query.strip()}%"
                    cur.execute(
                        """
                        SELECT id, session_id, timestamp, role, content, tool_name, tool_result
                        FROM turns
                        WHERE content LIKE ?
                        ORDER BY id DESC LIMIT ?
                        """,
                        (like_pat, limit),
                    )
                    return [dict(r) for r in cur.fetchall()]
                except Exception:
                    return []
            finally:
                conn.close()

    def get_recent_turns(self, n: int = 10, session_id: str | None = None) -> list[dict]:
        """Fetch the latest n turns."""
        with _lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                if session_id:
                    cur.execute(
                        "SELECT * FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                        (session_id, n),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM turns ORDER BY id DESC LIMIT ?",
                        (n,),
                    )
                rows = cur.fetchall()
                return [dict(r) for r in reversed(rows)]
            finally:
                conn.close()

    def get_recent_sessions(self, limit: int = 10) -> list[dict]:
        """Fetch latest sessions with summaries."""
        with _lock:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()

# Singleton instance
_history_instance: ConversationHistory | None = None

def get_history() -> ConversationHistory:
    global _history_instance
    if _history_instance is None:
        _history_instance = ConversationHistory()
    return _history_instance
