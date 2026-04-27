"""
State Management Module

Provides persistent session storage using SQLite with WAL mode for
concurrency. Manages conversation history and session metadata.

Design choices:
- WAL mode for concurrent readers + single writer
- JSON-serialized message history for flexibility
- Session-level isolation for multi-user support
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# =============================================================================
# Schema Definition
# =============================================================================

SCHEMA_VERSION = 1

CREATE_TABLES_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Sessions table: metadata for each conversation
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    metadata TEXT
);

-- Messages table: full conversation history
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    metadata TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""


class Database:
    """
    SQLite-backed database for session and message storage.

    Thread-safe for the common pattern of multiple readers
    and single writer. Uses WAL mode for better concurrency.

    Security notes:
    - All user content is stored as TEXT (never evaluated)
    - Parameterized queries prevent SQL injection
    - WAL mode prevents writer blocking readers
    """

    def __init__(self, db_path: str):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

        self._connect()
        self._init_schema()

    def _connect(self) -> None:
        """Establish database connection with optimal settings."""
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,  # Autocommit mode
        )
        self._conn.row_factory = sqlite3.Row

        # Performance and safety settings
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")

    def _init_schema(self) -> None:
        """Initialize database schema, creating tables if needed."""
        cursor = self._conn.cursor()

        # Create tables
        cursor.executescript(CREATE_TABLES_SQL)

        # Check and update schema version
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()

        if row is None:
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,)
            )
            self._conn.commit()
        else:
            current_version = row[0] if isinstance(row, sqlite3.Row) else row["version"]
            if current_version < SCHEMA_VERSION:
                logger.info(
                    "Database schema version %d < current %d, migration needed",
                    current_version, SCHEMA_VERSION
                )
                cursor.execute(
                    "UPDATE schema_version SET version = ?",
                    (SCHEMA_VERSION,)
                )
                self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    @contextmanager
    def transaction(self):
        """
        Context manager for database transactions.

        Handles BEGIN/COMMIT/ROLLBACK automatically.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # =========================================================================
    # Session Operations
    # =========================================================================

    def create_session(
        self,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a new session.

        Args:
            session_id: Optional custom session ID. Generated if not provided.
            title: Optional session title.
            metadata: Optional additional metadata.

        Returns:
            The session ID.
        """
        if session_id is None:
            session_id = str(uuid.uuid4())

        now = time.time()

        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO sessions (id, title, created_at, updated_at, metadata)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    session_id,
                    title,
                    now,
                    now,
                    json.dumps(metadata) if metadata else None,
                )
            )

        logger.debug("Created session: %s", session_id)
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get session metadata.

        Args:
            session_id: The session ID to look up.

        Returns:
            Session dict or None if not found.
        """
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,)
            )
            row = cursor.fetchone()

        if row:
            return dict(row)
        return None

    def update_session(
        self,
        session_id: str,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        message_count: Optional[int] = None,
        tool_call_count: Optional[int] = None,
    ) -> bool:
        """
        Update session metadata.

        Args:
            session_id: The session ID to update.
            title: New title (if provided).
            metadata: New metadata (if provided).
            message_count: Update message count.
            tool_call_count: Update tool call count.

        Returns:
            True if session was found and updated.
        """
        updates = []
        params = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)

        if metadata is not None:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata))

        if message_count is not None:
            updates.append("message_count = ?")
            params.append(message_count)

        if tool_call_count is not None:
            updates.append("tool_call_count = ?")
            params.append(tool_call_count)

        if not updates:
            return True

        updates.append("updated_at = ?")
        params.append(time.time())

        params.append(session_id)

        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
                params
            )
            return cursor.rowcount > 0

    def end_session(self, session_id: str) -> bool:
        """
        Mark a session as ended.

        Args:
            session_id: The session ID to end.

        Returns:
            True if session was found and ended.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET ended_at = ?, updated_at = ? WHERE id = ? AND ended_at IS NULL",
                (time.time(), time.time(), session_id)
            )
            return cursor.rowcount > 0

    def list_sessions(
        self,
        limit: int = 20,
        offset: int = 0,
        include_ended: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List sessions, most recent first.

        Args:
            limit: Maximum number of sessions to return.
            offset: Number of sessions to skip.
            include_ended: Whether to include ended sessions.

        Returns:
            List of session dictionaries.
        """
        with self._lock:
            if include_ended:
                cursor = self._conn.execute(
                    """SELECT * FROM sessions
                       ORDER BY updated_at DESC
                       LIMIT ? OFFSET ?""",
                    (limit, offset)
                )
            else:
                cursor = self._conn.execute(
                    """SELECT * FROM sessions
                       WHERE ended_at IS NULL
                       ORDER BY updated_at DESC
                       LIMIT ? OFFSET ?""",
                    (limit, offset)
                )
            return [dict(row) for row in cursor.fetchall()]

    def delete_session(self, session_id: str) -> bool:
        """
        Delete a session and all its messages.

        Args:
            session_id: The session ID to delete.

        Returns:
            True if session was found and deleted.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE id = ?",
                (session_id,)
            )
            return cursor.rowcount > 0

    # =========================================================================
    # Message Operations
    # =========================================================================

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        tool_call_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Add a message to a session.

        Args:
            session_id: The session to add to.
            role: Message role (user/assistant/system/tool).
            content: Message text content.
            tool_calls: Optional tool calls list.
            tool_call_id: ID of tool call this responds to.
            tool_name: Name of the tool.
            metadata: Optional additional metadata.

        Returns:
            The message row ID.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO messages
                   (session_id, role, content, tool_calls, tool_call_id, tool_name, timestamp, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    content,
                    json.dumps(tool_calls) if tool_calls else None,
                    tool_call_id,
                    tool_name,
                    time.time(),
                    json.dumps(metadata) if metadata else None,
                )
            )

            # Update session message count and timestamp
            tool_call_increment = 1 if tool_calls else 0
            conn.execute(
                """UPDATE sessions
                   SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ?,
                       updated_at = ?
                   WHERE id = ?""",
                (tool_call_increment, time.time(), session_id)
            )

            return cursor.lastrowid or 0

    def get_messages(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get all messages for a session.

        Args:
            session_id: The session ID.
            limit: Optional limit on number of messages.

        Returns:
            List of message dictionaries.
        """
        with self._lock:
            if limit:
                cursor = self._conn.execute(
                    """SELECT * FROM messages
                       WHERE session_id = ?
                       ORDER BY timestamp ASC
                       LIMIT ?""",
                    (session_id, limit)
                )
            else:
                cursor = self._conn.execute(
                    """SELECT * FROM messages
                       WHERE session_id = ?
                       ORDER BY timestamp ASC""",
                    (session_id,)
                )

            messages = []
            for row in cursor.fetchall():
                msg = dict(row)
                # Deserialize JSON fields
                if msg.get("tool_calls"):
                    try:
                        msg["tool_calls"] = json.loads(msg["tool_calls"])
                    except (json.JSONDecodeError, TypeError):
                        msg["tool_calls"] = []
                if msg.get("metadata"):
                    try:
                        msg["metadata"] = json.loads(msg["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        msg["metadata"] = {}
                messages.append(msg)

            return messages

    def clear_messages(self, session_id: str) -> int:
        """
        Delete all messages from a session.

        Args:
            session_id: The session ID.

        Returns:
            Number of messages deleted.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM messages WHERE session_id = ?",
                (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
                (session_id,)
            )
            return cursor.rowcount

    def search_messages(
        self,
        query: str,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search messages by content.

        Args:
            query: Search query string.
            session_id: Optional session filter.
            limit: Maximum results.

        Returns:
            List of matching message dictionaries.
        """
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    """SELECT * FROM messages
                       WHERE session_id = ? AND content LIKE ?
                       ORDER BY timestamp DESC
                       LIMIT ?""",
                    (session_id, f"%{query}%", limit)
                )
            else:
                cursor = self._conn.execute(
                    """SELECT * FROM messages
                       WHERE content LIKE ?
                       ORDER BY timestamp DESC
                       LIMIT ?""",
                    (f"%{query}%", limit)
                )

            return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Utility Operations
    # =========================================================================

    def get_stats(self) -> Dict[str, int]:
        """
        Get database statistics.

        Returns:
            Dict with session_count and message_count.
        """
        with self._lock:
            session_cursor = self._conn.execute(
                "SELECT COUNT(*) FROM sessions"
            )
            session_count = session_cursor.fetchone()[0]

            message_cursor = self._conn.execute(
                "SELECT COUNT(*) FROM messages"
            )
            message_count = message_cursor.fetchone()[0]

        return {
            "session_count": session_count,
            "message_count": message_count,
        }

    def vacuum(self) -> None:
        """Optimize database by reclaiming unused space."""
        with self._lock:
            self._conn.execute("VACUUM")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class SessionState:
    """
    Manages the state for a single conversation session.

    Provides a high-level interface to the database
    and in-memory state management.
    """

    def __init__(self, session_id: Optional[str] = None, db: Optional[Database] = None):
        """
        Initialize session state.

        Args:
            session_id: Optional session ID. Creates new if not provided.
            db: Optional Database instance. Creates new if not provided.
        """
        self.db = db or self._get_default_db()
        self.session_id = session_id or self.db.create_session()

        # In-memory message cache for current session
        self._message_cache: List[Dict[str, Any]] = []
        self._cache_loaded = False

    @classmethod
    def _get_default_db(cls) -> Database:
        """Get or create the default database instance."""
        if not hasattr(cls, "_default_db_instance"):
            from phoenix_agent.core.config import get_config
            config = get_config()
            cls._default_db_instance = Database(config.storage.db_path)
        return cls._default_db_instance

    def load_messages(self) -> None:
        """Load messages from database into memory cache."""
        if self._cache_loaded:
            return

        self._message_cache = self.db.get_messages(self.session_id)
        self._cache_loaded = True

    def get_messages(self) -> List[Dict[str, Any]]:
        """Get all messages for this session."""
        self.load_messages()
        return self._message_cache.copy()

    def add_message(self, role: str, content: str = "", **kwargs) -> int:
        """
        Add a message to this session.

        Args:
            role: Message role.
            content: Message content.
            **kwargs: Additional message fields.

        Returns:
            Message row ID.
        """
        self.load_messages()

        msg_id = self.db.add_message(
            session_id=self.session_id,
            role=role,
            content=content,
            **kwargs
        )

        # Update cache
        self._message_cache.append({
            "id": msg_id,
            "session_id": self.session_id,
            "role": role,
            "content": content,
            **kwargs
        })

        return msg_id

    def get_metadata(self) -> Dict[str, Any]:
        """Get session metadata."""
        return self.db.get_session(self.session_id) or {}

    def update_title(self, title: str) -> bool:
        """Update session title."""
        return self.db.update_session(self.session_id, title=title)

    def clear(self) -> int:
        """Clear all messages in this session."""
        count = self.db.clear_messages(self.session_id)
        self._message_cache.clear()
        return count

    def end(self) -> bool:
        """Mark this session as ended."""
        return self.db.end_session(self.session_id)

    def delete(self) -> bool:
        """Delete this session."""
        return self.db.delete_session(self.session_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass  # Don't close shared database
