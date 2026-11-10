"""Простые настройки per-chat (sqlite kv)."""
import os
import sqlite3
from contextlib import contextmanager

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("KV_DB", os.path.join(_BASE, "kv.db"))


@contextmanager
def _connect():
    """with _connect() as conn: — закрывает соединение (иначе утечка FD)."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS kv (
                chat_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (chat_id, key)
            )"""
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def get(chat_id: int, key: str, default: str | None = None) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM kv WHERE chat_id = ? AND key = ?", (chat_id, key)
        ).fetchone()
        return row[0] if row else default


def set(chat_id: int, key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO kv (chat_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET value = excluded.value",
            (chat_id, key, value),
        )


def voice_enabled(chat_id: int) -> bool:
    return get(chat_id, "voice") == "on"


def voice_key(chat_id: int) -> str:
    return get(chat_id, "voice_voice", "dmitry") or "dmitry"
