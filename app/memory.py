"""Долговременная память бота: факты владельца, история диалога, FTS5-поиск по заметкам."""
import os
import re
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("MEMORY_DB", "memory.db")

DIALOG_KEEP = 200  # сколько реплик храним на чат


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    return conn


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
    """Поиск по заметкам: FTS5, при пустом результате — LIKE-фолбэк."""
    sync_notes(notes_dir)
    q = _fts_query(query)
    rows: list[sqlite3.Row] = []
    if q:
        with _connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT path, snippet(notes_fts, 1, '', ' … ', '', 24) AS snip "
                    "FROM notes_fts WHERE notes_fts MATCH ? LIMIT ?",
                    (q, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
    if not rows:
        token = (re.findall(r"[а-яёa-z0-9]{3,}", query.lower()) or [""])[0]
        if token:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT path, substr(body, 1, 300) AS snip FROM notes_fts "
                    "WHERE lower(body) LIKE ? LIMIT ?",
                    (f"%{token}%", limit),
                ).fetchall()
    out = []
    for r in rows:
        snippet = " ".join(str(r["snip"]).split())
        out.append({"path": r["path"], "snippet": snippet})
    return out
