"""Petit index SQLite : emails déjà traités (anti-doublon) et fils connus (pour retrouver les validations en attente)."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class StateIndex:
    def __init__(self, path: Path):
        self.path = path
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS processed (message_id TEXT PRIMARY KEY, thread_id TEXT, ts REAL)")
            c.execute("CREATE TABLE IF NOT EXISTS threads (thread_id TEXT PRIMARY KEY, ts REAL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def is_processed(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._conn() as c:
            return c.execute("SELECT 1 FROM processed WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_processed(self, message_id: str, thread_id: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO threads VALUES (?,?)", (thread_id, time.time()))
            if message_id:
                c.execute("INSERT OR IGNORE INTO processed VALUES (?,?,?)", (message_id, thread_id, time.time()))

    def threads(self) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT thread_id FROM threads ORDER BY ts")]

    def messages_for_thread(self, thread_id: str) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT message_id FROM processed WHERE thread_id=? ORDER BY ts", (thread_id,))]
