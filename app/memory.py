"""Долговременная память бота: факты владельца, история диалога, FTS5-поиск по заметкам."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("MEMORY_DB", os.path.join(_BASE, "memory.db"))

DIALOG_KEEP = 200  # сколько реплик храним на чат


@contextmanager
def _connect():
    """with _connect() as conn: — закрывает соединение (иначе утечка FD)."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                fact TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS dialog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                ts TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(path UNINDEXED, body)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notes_files (path TEXT PRIMARY KEY, mtime REAL NOT NULL)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )"""
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- пользователи (кто заходил в бота) ----------

def upsert_user(
    user_id: int,
    username: str = "",
    first_name: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username = COALESCE(NULLIF(excluded.username, ''), users.username),
                 first_name = COALESCE(NULLIF(excluded.first_name, ''), users.first_name),
                 last_seen = excluded.last_seen""",
            (int(user_id), username or "", first_name or "", now, now),
        )


def list_users(limit: int = 50) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()


def import_users_from_dialog() -> int:
    """Дозаписать chat_id из истории диалога (исторические сессии до таблицы users)."""
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM dialog WHERE chat_id NOT IN (SELECT user_id FROM users)"
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, first_seen, last_seen) "
                "VALUES (?, '', '', ?, ?)",
                (r["chat_id"], now, now),
            )
            n += 1
    return n


# ---------- факты ----------

def add_fact(chat_id: int, fact: str, source: str = "manual") -> int:
    fact = fact.strip()
    if not fact:
        return 0
    # дедуп: не плодим одинаковые факты
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM facts WHERE chat_id = ? AND lower(fact) = lower(?)",
            (chat_id, fact),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO facts (chat_id, fact, source, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, fact, source, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def list_facts(chat_id: int, limit: int = 50) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT fact FROM facts WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [r["fact"] for r in rows]


def facts_context(chat_id: int, limit: int = 40) -> str:
    facts = list_facts(chat_id, limit)
    if not facts:
        return ""
    return "Факты о пользователе:\n" + "\n".join(f"- {f}" for f in facts)


# ---------- история диалога ----------

def add_dialog(chat_id: int, role: str, text: str) -> None:
    text = text.strip()
    if not text:
        return
    with _connect() as conn:
        conn.execute(
            "INSERT INTO dialog (chat_id, role, text, ts) VALUES (?, ?, ?, ?)",
            (chat_id, role, text[:1000], datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "DELETE FROM dialog WHERE chat_id = ? AND id NOT IN ("
            "  SELECT id FROM dialog WHERE chat_id = ? ORDER BY id DESC LIMIT ?)",
            (chat_id, chat_id, DIALOG_KEEP),
        )


def get_history(chat_id: int, limit: int = 12) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, text FROM dialog WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def history_context(chat_id: int, limit: int = 12) -> str:
    rows = get_history(chat_id, limit)
    if not rows:
        return ""
    label = {"user": "Пользователь", "assistant": "Ассистент"}
    lines = [
        f"- {label.get(r['role'], r['role'])}: {r['text'][:200]}" for r in rows
    ]
    return "Последние реплики диалога:\n" + "\n".join(lines)


# ---------- заметки: FTS5 ----------

def sync_notes(notes_dir: str) -> None:
    """Индексирует *.md в notes_dir (обновляет по mtime, удаляет пропавшие)."""
    files = {}
    if os.path.isdir(notes_dir):
        for name in os.listdir(notes_dir):
            if name.endswith(".md"):
                path = os.path.join(notes_dir, name)
                try:
                    files[path] = os.path.getmtime(path)
                except OSError:
                    continue
    with _connect() as conn:
        known = {
            r["path"]: r["mtime"]
            for r in conn.execute("SELECT path, mtime FROM notes_files").fetchall()
        }
        for path, mtime in files.items():
            if known.get(path) == mtime:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    body = f.read()
            except OSError:
                continue
            conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
            conn.execute(
                "INSERT INTO notes_fts (path, body) VALUES (?, ?)", (path, body)
            )
            conn.execute(
                "INSERT INTO notes_files (path, mtime) VALUES (?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime",
                (path, mtime),
            )
        for path in known:
            if path not in files:
                conn.execute("DELETE FROM notes_fts WHERE path = ?", (path,))
                conn.execute("DELETE FROM notes_files WHERE path = ?", (path,))


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[а-яёa-z0-9]{3,}", query.lower())
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(tokens))


def search_notes(notes_dir: str, query: str, limit: int = 4) -> list[dict]:
    """Поиск по заметкам: FTS5, при пустом результате — LIKE-фолбэк.
    Фильтрует по префиксу notes_dir — чужие папки не попадают."""
    sync_notes(notes_dir)
    prefix = notes_dir.rstrip("/") + "/"
    q = _fts_query(query)
    rows: list[sqlite3.Row] = []
    if q:
        with _connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT path, snippet(notes_fts, 1, '', ' … ', '', 24) AS snip "
                    "FROM notes_fts WHERE notes_fts MATCH ? AND substr(path, 1, length(?)) = ? "
                    "LIMIT ?",
                    (q, prefix, prefix, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
    if not rows:
        token = (re.findall(r"[а-яёa-z0-9]{3,}", query.lower()) or [""])[0]
        if token:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT path, substr(body, 1, 300) AS snip FROM notes_fts "
                    "WHERE lower(body) LIKE ? AND substr(path, 1, length(?)) = ? LIMIT ?",
                    (f"%{token}%", prefix, prefix, limit),
                ).fetchall()
    out = []
    for r in rows:
        snippet = " ".join(str(r["snip"]).split())
        out.append({"path": r["path"], "snippet": snippet})
    return out
