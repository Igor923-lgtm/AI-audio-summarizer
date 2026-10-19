import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("REMINDERS_DB", "reminders.db")

# правила повтора: daily | weekdays | weekly:<0-6 (пн=0)> | monthly:<день 1-31>
WEEKDAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )"""
    )
    # миграция: колонка повтора (безопасно, если уже есть)
    try:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat_rule TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


def add_reminder(chat_id: int, text: str, due_utc: datetime, repeat_rule: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (chat_id, text, due_at, created_at, repeat_rule) VALUES (?, ?, ?, ?, ?)",
            (chat_id, text, due_utc.astimezone(timezone.utc).isoformat(), now, repeat_rule),
        )
        return cur.lastrowid


def get_due(now_utc: datetime) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ?",
            (now_utc.isoformat(),),
        ).fetchall()


def get_today(chat_id: int, day_start_utc: datetime, day_end_utc: datetime) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM reminders
               WHERE chat_id = ? AND status = 'pending' AND due_at >= ? AND due_at < ?
               ORDER BY due_at""",
            (chat_id, day_start_utc.isoformat(), day_end_utc.isoformat()),
        ).fetchall()


def mark_sent(reminder_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE reminders SET status = 'sent' WHERE id = ?", (reminder_id,))


def complete(reminder_id: int) -> None:
    """Готово: закрыть без повтора."""
    with _connect() as conn:
        conn.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (reminder_id,))


def snooze(reminder_id: int, new_due_utc: datetime) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE reminders SET due_at = ?, status = 'pending' WHERE id = ? AND status IN ('pending','sent','done')",
            (new_due_utc.astimezone(timezone.utc).isoformat(), reminder_id),
        )
        return cur.rowcount > 0


def list_pending(chat_id: int) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE chat_id = ? AND status = 'pending' ORDER BY due_at",
            (chat_id,),
        ).fetchall()


def cancel(reminder_id: int, chat_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND chat_id = ? AND status = 'pending'",
            (reminder_id, chat_id),
        )
        return cur.rowcount > 0


def next_due(rule: str, current_due_msk: datetime) -> datetime | None:
    """Следующий срок по правилу повтора. current_due — MSK aware datetime."""
    if not rule:
        return None
    if rule == "daily":
        return current_due_msk + timedelta(days=1)
    if rule == "weekdays":
        d = current_due_msk + timedelta(days=1)
        while d.weekday() >= 5:  # сб/вс → пн
            d += timedelta(days=1)
        return d
    if rule.startswith("weekly:"):
        wd = int(rule.split(":", 1)[1]) % 7
        d = current_due_msk + timedelta(days=1)
        while d.weekday() != wd:
            d += timedelta(days=1)
        return d
    if rule.startswith("monthly:"):
        day = min(int(rule.split(":", 1)[1]), 28)
        d = current_due_msk.replace(day=day) + timedelta(days=0)
        if d <= current_due_msk:
            # следующий месяц
            if d.month == 12:
                d = d.replace(year=d.year + 1, month=1, day=day)
            else:
                d = d.replace(month=d.month + 1, day=day)
        return d
    return None
