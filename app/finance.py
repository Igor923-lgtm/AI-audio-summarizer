import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("FINANCE_DB", os.path.join(_BASE, "finance.db"))


@contextmanager
def _connect():
    """with _connect() as conn: — закрывает соединение (иначе утечка FD)."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS finance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                comment TEXT,
                occurred_on TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_operation(chat_id: int, amount: float, category: str, comment: str, occurred_on: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO finance (chat_id, amount, category, comment, occurred_on, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, amount, category, comment, occurred_on, now),
        )
        return cur.lastrowid


def month_summary(chat_id: int, month: str) -> dict:
    """month: YYYY-MM (MSK). Возвращает income, expense, balance."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0) AS income,
                 COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) AS expense
               FROM finance WHERE chat_id = ? AND occurred_on LIKE ?""",
            (chat_id, month + "%"),
        ).fetchone()
        return {"income": row["income"], "expense": row["expense"], "balance": row["income"] - row["expense"]}


def month_by_category(chat_id: int, month: str) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT category,
                      SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS income,
                      SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS expense
               FROM finance WHERE chat_id = ? AND occurred_on LIKE ?
               GROUP BY category ORDER BY expense DESC""",
            (chat_id, month + "%"),
        ).fetchall()


def recent(chat_id: int, limit: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM finance WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
