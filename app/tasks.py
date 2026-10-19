"""Задачи: простой to-do с кнопками."""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("TASKS_DB", "tasks.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            done_at TEXT
        )"""
    )
    return conn


def add_task(chat_id: int, text: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (chat_id, text, created_at) VALUES (?, ?, ?)",
            (chat_id, text, now),
        )
        return cur.lastrowid


def set_done(task_id: int, chat_id: int, done: bool) -> bool:
    now = datetime.now(timezone.utc).isoformat() if done else None
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET done = ?, done_at = ? WHERE id = ? AND chat_id = ?",
            (1 if done else 0, now, task_id, chat_id),
        )
        return cur.rowcount > 0


def list_tasks(chat_id: int, done: bool = False) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND done = ? ORDER BY id DESC LIMIT 20",
            (chat_id, 1 if done else 0),
        ).fetchall()


def count_active(chat_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE chat_id = ? AND done = 0", (chat_id,)
        ).fetchone()
        return row["c"]
