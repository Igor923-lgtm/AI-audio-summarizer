"""Единый слой визуала: HTML-рендер, клавиатуры, карточки, пагинация.

Весь текст в чате — только HTML (parse_mode бота). Markdown не используется.
"""
import html
import re

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)


def esc(s: str) -> str:
    return html.escape(str(s or ""))


def card(title: str, body: str = "", emoji: str = "") -> str:
    """Аккуратная карточка: заголовок жирным, тело с абзацами."""
    head = f"{emoji} <b>{esc(title)}</b>" if emoji else f"<b>{esc(title)}</b>"
    if body:
        return f"{head}\n{body}"
    return head


def main_kb() -> ReplyKeyboardMarkup:
    """Постоянное главное меню."""
    kb = [
        [KeyboardButton(text="📝 Заметки"), KeyboardButton(text="⏰ Напоминания")],
        [KeyboardButton(text="✅ Задачи"), KeyboardButton(text="💸 Финансы")],
        [KeyboardButton(text="📅 Календарь"), KeyboardButton(text="⚙️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def hide_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """rows: [[(label, callback_data), ...], ...]"""
    keyboard = [
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ---------- Разделы меню ----------

def notes_menu_kb() -> InlineKeyboardMarkup:
    return inline([[("🕑 Последние", "ui:notes:last"), ("🔍 Поиск", "ui:notes:search")]])


def note_pager_kb(idx: int, total: int) -> InlineKeyboardMarkup:
    row: list[tuple[str, str]] = []
    if idx > 0:
        row.append(("⬅️ Назад", f"ui:notes:prev:{idx - 1}"))
    row.append((f"{idx + 1}/{total}", "ui:noop"))
    if idx < total - 1:
        row.append(("Вперёд ➡️", f"ui:notes:next:{idx + 1}"))
    row2 = [("📄 Файл", f"ui:notes:file:{idx}"), ("🏠 Меню", "ui:menu")]
    return inline([row, row2])


def reminders_menu_kb() -> InlineKeyboardMarkup:
    return inline(
        [
            [("📅 Сегодня", "ui:rem:today"), ("📋 Все", "ui:rem:all")],
            [("ℹ️ Как создать", "ui:rem:help"), ("🏠 Меню", "ui:menu")],
        ]
    )


def reminder_actions_kb(rid: int) -> InlineKeyboardMarkup:
    return inline(
        [
            [("✅ Готово", f"ui:rem:done:{rid}")],
            [("⏰ +1 час", f"ui:rem:snooze:{rid}:3600"), ("📅 Завтра", f"ui:rem:snooze:{rid}:86400")],
        ]
    )


def reminder_item_kb(rid: int) -> InlineKeyboardMarkup:
    return inline([[("❌ Отмена", f"ui:rem:cancel:{rid}"), ("🏠 Меню", "ui:menu")]])


def tasks_menu_kb() -> InlineKeyboardMarkup:
    return inline(
        [
            [("🔓 Активные", "ui:tasks:active"), ("✅ Сделанные", "ui:tasks:done")],
            [("🏠 Меню", "ui:menu")],
        ]
    )


def task_item_kb(tid: int, done: bool) -> InlineKeyboardMarkup:
    if done:
        return inline([[("↩️ Вернуть", f"ui:tasks:undone:{tid}"), ("🏠 Меню", "ui:menu")]])
    return inline([[("✔️ Готово", f"ui:tasks:done:{tid}"), ("🏠 Меню", "ui:menu")]])


def finance_menu_kb() -> InlineKeyboardMarkup:
    return inline(
        [
            [("💰 Баланс", "ui:fin:balance"), ("📊 Отчёт", "ui:fin:report")],
            [("🧾 Операции", "ui:fin:list"), ("🏠 Меню", "ui:menu")],
        ]
    )


def help_kb(voice_on: bool = False, voice_key: str = "dmitry") -> InlineKeyboardMarkup:
    state = "выкл 🔇" if not voice_on else "вкл 🔊"
    voice_name = "Светлана" if voice_key == "svetlana" else "Дмитрий"
    return inline(
        [
            [(f"🔊 Голос: {state}", "ui:voice:toggle")],
            [(f"🎙 {voice_name}", "ui:voice:dmitry" if voice_key != "dmitry" else "ui:voice:svetlana")],
            [("🏠 Меню", "ui:menu")],
        ]
    )


# ---------- Рендер markdown-заметки в HTML (только для чата) ----------

def note_to_html(md: str) -> str:
    """Конвертирует заметку LLM (markdown) в красивый HTML для чата."""
    lines = md.splitlines()
    out: list[str] = []
    list_mode = False
    title_done = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if list_mode:
                out.append("</blockquote>")
                list_mode = False
            out.append("")
            continue
        # заголовки разделов
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if list_mode:
                out.append("</blockquote>")
                list_mode = False
            out.append(f"\n<b>▎{esc(m.group(1))}</b>")
            continue
        # главный заголовок
        m = re.match(r"^#\s+(.*)$", line)
        if m:
            out.append(f"🔖 <b>{esc(m.group(1))}</b>")
            title_done = True
            continue
        # чекбоксы задач
        m = re.match(r"^-\s+\[[ xX]\]\s+(.*)$", line)
        if m:
            if not list_mode:
                out.append("<blockquote>")
                list_mode = True
            out.append(f"▫️ {esc(m.group(1))}")
            continue
        # маркеры
        m = re.match(r"^[-*]\s+(.*)$", line)
        if m:
            if not list_mode:
                out.append("<blockquote>")
                list_mode = True
            out.append(f"• {_inline_bold(m.group(1))}")
            continue
        if list_mode:
            out.append("</blockquote>")
            list_mode = False
        out.append(_inline_bold(line))
    if list_mode:
        out.append("</blockquote>")
    text = "\n".join(out).strip()
    return text


def _inline_bold(s: str) -> str:
    """**bold** / *italic* внутри строки → HTML (после экранирования)."""
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def notes_index(note_files: list[str]) -> list[str]:
    """Заголовки заметок для пагинации: первая строка '# ...' или первая осмысленная."""
    titles = []
    for path in note_files:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip().lstrip("#").strip()
                    if line:
                        titles.append(line[:60])
                        break
                else:
                    titles.append("(пусто)")
        except Exception:
            titles.append("(не читается)")
    return titles
