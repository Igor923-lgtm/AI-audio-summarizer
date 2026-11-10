"""Задачи: простой to-do с кнопками."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("TASKS_DB", os.path.join(_BASE, "tasks.db"))


@contextmanager
def _connect():
    """with _connect() as conn: — закрывает соединение (иначе утечка FD)."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
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
        yield conn
        conn.commit()
    finally:
        conn.close()


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


def list_tasks_for_day(chat_id: int, day_utc_start, day_utc_end) -> list[sqlite3.Row]:
    """Задачи, созданные или закрытые в интервал [start, end) (UTC ISO)."""
    start = day_utc_start.isoformat()
    end = day_utc_end.isoformat()
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM tasks
               WHERE chat_id = ?
                 AND (created_at >= ? AND created_at < ?
                      OR (done_at IS NOT NULL AND done_at >= ? AND done_at < ?))
               ORDER BY id DESC LIMIT 20""",
            (chat_id, start, end, start, end),
        ).fetchall()


def get_task(task_id: int, chat_id: int):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND chat_id = ?",
            (task_id, chat_id),
        ).fetchone()


def set_text(task_id: int, chat_id: int, text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET text = ? WHERE id = ? AND chat_id = ?",
            (text, task_id, chat_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int, chat_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND chat_id = ?",
            (task_id, chat_id),
        )
        return cur.rowcount > 0
