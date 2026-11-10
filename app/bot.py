import calendar as cal_stdlib
import os
import re
import logging
import asyncio
from datetime import datetime, date as date_cls, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from app.config import settings
from app.audio_service import AudioService
from app.transcribe_service import TranscribeService
from app.ai_service import AIService
from app import reminders as reminders_db
from app import finance as finance_db
from app import tasks as tasks_db
from app import kv
from app import tts
from app import ui
from app import memory
from app import tools_search
from app import skills

logger = logging.getLogger(__name__)

TZ_MSK = timezone(timedelta(hours=3), name="Europe/Moscow")

bot = Bot(
    token=settings.telegram_bot_token.get_secret_value(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

transcribe_service = TranscribeService()
ai_service = AIService()

DOWNLOADS_DIR = "downloads"
NOTES_DIR = settings.notes_dir
# только для владельца: сводка/рефлексия; НЕ whitelist — бот открыт всем
ALLOWED_IDS = settings.allowed_ids

# черновик напоминания, извлечённого из последней длинной заметки: chat_id → {when, text, repeat}
_pending_note_rem: dict[int, dict] = {}


def _user_notes_dir(chat_id: int) -> str:
    """Персональная папка заметок: notes/<chat_id>/."""
    return os.path.join(NOTES_DIR, str(int(chat_id)))


def _ensure_user_notes(chat_id: int) -> str:
    d = _user_notes_dir(chat_id)
    os.makedirs(d, exist_ok=True)
    return d


def _migrate_root_notes_once() -> None:
    """Старые заметки в корне notes/ → notes/<владелец>/ (один раз при старте)."""
    if not os.path.isdir(NOTES_DIR):
        return
    owner = str(min(ALLOWED_IDS)) if ALLOWED_IDS else "294308582"
    root_md = [
        f
        for f in os.listdir(NOTES_DIR)
        if f.endswith(".md") and os.path.isfile(os.path.join(NOTES_DIR, f))
    ]
    if not root_md:
        return
    dest = os.path.join(NOTES_DIR, owner)
    os.makedirs(dest, exist_ok=True)
    for f in root_md:
        src = os.path.join(NOTES_DIR, f)
        dst = os.path.join(dest, f)
        if not os.path.exists(dst):
            os.replace(src, dst)
    logger.info(f"Миграция заметок: {len(root_md)} файлов → notes/{owner}/")


_migrate_root_notes_once()


# открытый доступ: бот отвечает всем; изоляция — папки/chat_id
@dp.update.outer_middleware()
async def whitelist_middleware(handler, event, data):
    # тихий учёт кто заходил (для /users у владельца)
    try:
        user = None
        if hasattr(event, "message") and event.message:
            user = event.message.from_user
        elif hasattr(event, "callback_query") and event.callback_query:
            user = event.callback_query.from_user
        if user and not user.is_bot:
            memory.upsert_user(
                user.id,
                username=user.username or "",
                first_name=user.first_name or "",
            )
    except Exception as e:
        logger.warning(f"upsert_user failed: {e}")
    return await handler(event, data)


# ---------- утилиты ----------

def _sanitize_filename(name: str) -> str:
    name = os.path.splitext(name)[0]
    clean = re.sub(r"[^a-zA-Z0-9_\-а-яА-ЯіІїЇєЄ]", "_", name).strip("_")
    return clean or "note"


def _save_note(markdown_report: str, display_name: str, chat_id: int = 0) -> str:
    target = _ensure_user_notes(chat_id or 0)
    from datetime import datetime as _dt

    stamp = _dt.now().strftime("%Y-%m-%d_%H%M%S")
    # осмысленный суффикс из первых слов; fallback — display_name (заметка_<tg-id>)
    title = _note_title_from_md(markdown_report, fallback="")
    suffix = _sanitize_filename(title) if title else _sanitize_filename(display_name)
    file_name = f"{stamp}_{suffix}.md"
    file_path = os.path.join(target, file_name)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(markdown_report)
    logger.info(f"Заметка сохранена: {file_path}")
    return file_path


def _list_notes(chat_id: int = 0) -> list[str]:
    d = _user_notes_dir(chat_id)
    if not os.path.isdir(d):
        return []
    return sorted(
        (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")),
        key=os.path.getmtime,
        reverse=True,
    )


def _safe_user_note_path(chat_id: int, basename: str) -> str:
    """Путь только внутри папки пользователя (защита от ../ и чужих папок)."""
    base = os.path.basename(basename)
    return os.path.join(_user_notes_dir(chat_id), base)


def _fmt_when_msk(due_utc_iso: str) -> str:
    d = datetime.fromisoformat(due_utc_iso).astimezone(TZ_MSK)
    return d.strftime("%d.%m · %H:%M")


def _is_today_msk(due_utc_iso: str) -> bool:
    d = datetime.fromisoformat(due_utc_iso).astimezone(TZ_MSK)
    return d.date() == datetime.now(TZ_MSK).date()


def _money(amount: float) -> str:
    """Сумма с единым Unicode-минусом: −500 ₽ / +50000 ₽."""
    sign = "+" if amount > 0 else "−"
    return f"{sign}{abs(amount):.0f} ₽"


_MONTHS_NOM = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def _month_label(month: str) -> str:
    """'2026-10' → 'октябрь 2026' (название месяца вместо ISO)."""
    try:
        y, m = month.split("-")
        return f"{_MONTHS_NOM[int(m) - 1]} {y}"
    except (ValueError, AttributeError, IndexError):
        return month


def _date_short(iso_date: str) -> str:
    """'2026-10-19' → '19.10'."""
    try:
        return datetime.strptime(str(iso_date)[:10], "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        return str(iso_date)


def _display_name(path: str) -> str:
    """'2026-10-17_181500_Встреча_со_Светой.md' → 'Встреча со Светой.md'.

    Убирает технический префикс времени (чтобы имя не ломалось посередине)
    и делает подчёркивания пробелами. Сам файл не меняется — только подпись.
    """
    base = os.path.basename(path)
    base = re.sub(r"^\d{4}-\d{2}-\d{2}_\d{6}_", "", base)
    return base.replace("_", " ")


def _note_title_from_md(md: str, fallback: str = "") -> str:
    """Первые осмысленные слова заметки как заголовок (~60 символов)."""
    text = re.sub(r"<scratchpad>.*?</scratchpad>", "", md or "", flags=re.DOTALL)
    # 1) H1, если есть: «# План запуска»
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"^#\s+\S", line):
            t = re.sub(r"^#\s+", "", line)
            t = re.sub(r"[*_`]", "", t).strip()
            if len(t) >= 3:
                words = t.split()
                title = ""
                for w in words:
                    cand = (title + " " + w).strip()
                    if len(cand) > 60:
                        break
                    title = cand
                return title + ("…" if len(t) > len(title) else "")
    # 2) первая содержательная строка / буллет
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("<"):
            continue
        if line.startswith("- ") or line.startswith("* "):
            line = line[2:].strip()
        line = re.sub(r"[*_`>]", "", line).strip()
        if len(line) < 4:
            continue
        words = line.split()
        title = ""
        for w in words:
            cand = (title + " " + w).strip()
            if len(cand) > 60:
                break
            title = cand
        if len(title) >= 4:
            return title + ("…" if len(line) > len(title) else "")
    return fallback


def _note_title(path: str) -> str:
    """Заголовок заметки: первые слова содержимого, иначе имя файла."""
    fb = _display_name(path)
    try:
        with open(path, encoding="utf-8") as f:
            md = f.read(2000)
    except Exception:
        return fb
    return _note_title_from_md(md, fallback=fb)


def _repeat_label(rule: str | None) -> str:
    if not rule:
        return ""
    if rule == "daily":
        return " · 🔁 каждый день"
    if rule == "weekdays":
        return " · 🔁 по будням"
    if rule.startswith("weekly:"):
        wd = int(rule.split(":", 1)[1]) % 7
        return f" · 🔁 каждую {['пн','вт','ср','чт','пт','сб','вс'][wd]}"
    if rule.startswith("monthly:"):
        return f" · 🔁 ежемесячно {rule.split(':', 1)[1]}-го"
    return ""


async def _send_voice_reply(chat_id: int, plain: str) -> None:
    """Озвучка текста (edge-tts). Вызывается фоном — не блокирует текст."""
    try:
        audio = await tts.synthesize(plain, kv.voice_key(chat_id))
        if not audio:
            return
        await bot.send_voice(
            chat_id,
            types.BufferedInputFile(audio, filename="reply.mp3"),
        )
    except Exception as e:
        logger.error(f"Не удалось отправить голос: {e}")


async def deliver(message: types.Message, text: str, reply_markup=None):
    """Отправка ответа: всегда текст, при включённом голосе — ещё и голосовое.

    Голос уходит фоном (asyncio.create_task) — текст приходит сразу.
    """
    await message.answer(text, reply_markup=reply_markup)
    if kv.voice_enabled(message.chat.id):
        plain = re.sub(r"<[^>]+>", "", text)
        plain = re.sub(r"[^\S\n]+", " ", plain).strip()
        if plain:
            asyncio.create_task(_send_voice_reply(message.chat.id, plain))


# ---------- главное меню / старт / помощь ----------

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    first = (message.from_user.first_name if message.from_user else "") or "друг"
    await message.answer(
        ui.card(
            "Личный ассистент",
            f"Привет, {first}! Голосовое → чистая заметка. Напоминания, задачи и финансы — голосом или текстом.\n\n"
            "Выбери раздел кнопкой внизу 👇",
            "🤖",
        ),
        reply_markup=ui.main_kb(),
    )


def _help_text(on: bool) -> str:
    return (
        "⚙️ <b>Что умею</b>\n\n"
        "🎙 <b>Заметки</b> — голосовое → чистый текст без «эээ/ну/типа» + .md-файл\n"
        "⏰ <b>Напоминания</b> — «напомни завтра в 15:00 позвонить врачу», повторы: «каждый день...»\n"
        "✅ <b>Задачи</b> — «задача: сдать отчёт» → список с кнопкой «Готово»\n"
        "💸 <b>Финансы</b> — «потратил 500 на такси» → баланс и отчёт по категориям\n"
        f"🔊 <b>Голосовые ответы</b> — сейчас: {'включены' if on else 'выключены'}\n"
        "🧠 <b>Память</b> — «запомни, что ...» → помню; «что ты помнишь?» → список\n\n"
        "💬 Просто напиши любое сообщение — поддержу беседу и отвечу на вопросы.\n"
        "Меню всегда внизу экрана."
    )


@dp.message(Command("help"))
@dp.message(F.text == "⚙️ Помощь")
async def help_handler(message: types.Message):
    on = kv.voice_enabled(message.chat.id)
    await message.answer(_help_text(on), reply_markup=ui.help_kb(on, kv.voice_key(message.chat.id)))


@dp.callback_query(F.data == "ui:voice:toggle")
async def cb_voice_toggle(call: types.CallbackQuery):
    now = kv.voice_enabled(call.from_user.id)
    kv.set(call.from_user.id, "voice", "off" if now else "on")
    await call.answer("🔊 Голос включён" if not now else "🔇 Голос выключен")
    on = kv.voice_enabled(call.from_user.id)
    key = kv.voice_key(call.from_user.id)
    try:
        await call.message.edit_text(_help_text(on), reply_markup=ui.help_kb(on, key))
    except Exception:
        pass


@dp.callback_query(F.data == "ui:voice:dmitry")
@dp.callback_query(F.data == "ui:voice:svetlana")
async def cb_voice_pick(call: types.CallbackQuery):
    key = "svetlana" if call.data.endswith("svetlana") else "dmitry"
    kv.set(call.from_user.id, "voice_voice", key)
    name = "Светлана" if key == "svetlana" else "Дмитрий"
    await call.answer(f"🎙 Голос: {name}")
    try:
        await call.message.edit_reply_markup(
            reply_markup=ui.help_kb(kv.voice_enabled(call.from_user.id), key)
        )
    except Exception:
        pass


# ---------- главные кнопки ----------

@dp.message(F.text == "📝 Заметки")
async def notes_menu(message: types.Message):
    await message.answer(
        ui.card("Заметки", "Последние сохранённые или поиск по содержимому.", "📝"),
        reply_markup=ui.notes_menu_kb(),
    )


@dp.message(F.text == "⏰ Напоминания")
async def reminders_menu(message: types.Message):
    await message.answer(
        ui.card("Напоминания", "Активные напоминания и подсказка по созданию.", "⏰"),
        reply_markup=ui.reminders_menu_kb(),
    )


@dp.message(F.text == "✅ Задачи")
async def tasks_menu(message: types.Message):
    await message.answer(
        ui.card("Задачи", "Создавай: «задача: позвонить клиенту».", "✅"),
        reply_markup=ui.tasks_menu_kb(),
    )


@dp.message(F.text == "💸 Финансы")
async def finance_menu(message: types.Message):
    await message.answer(
        ui.card("Финансы", "Баланс месяца, категории, операции.", "💸"),
        reply_markup=ui.finance_menu_kb(),
    )


# ---------- КАЛЕНДАРЬ (сетка как в hairos при записи) ----------

# 1-индексный список (месяц 1..12) — НЕ затирать им _MONTHS_NOM:
# тот нужен _month_label с индексацией int(m)-1, иначе месяц сдвигается.
_MONTHS_CAL = [
    "", "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]
_DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _msk_day_to_utc_bounds(d: date_cls) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=TZ_MSK).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _notes_on_day(chat_id: int, d: date_cls) -> list[str]:
    """Заголовки (первые слова) заметок за день."""
    return [_note_title(p) for p in _note_paths_on_day(chat_id, d)]


def _note_paths_on_day(chat_id: int, d: date_cls) -> list[str]:
    prefix = d.strftime("%Y-%m-%d_")
    paths = []
    for path in _list_notes(chat_id):
        base = os.path.basename(path)
        if base.startswith(prefix) and base.endswith(".md"):
            paths.append(path)
    return paths


# кэш путей заметок дня для callback ncal_i_n_{idx}: (chat_id, "YYYY-MM-DD") → [paths]
_ncal_day_notes: dict[tuple[int, str], list[str]] = {}
_ncal_last_day: dict[int, str] = {}  # chat_id → последняя открытая дата
# ожидание нового текста: chat_id → {"kind": "task"|"rem", "id": int}
_edit_pending: dict[int, dict] = {}


def _events_for_range(chat_id: int, day_from: date_cls, day_to: date_cls) -> set[str]:
    """Множество 'YYYY-MM-DD', где есть хоть одно событие (напоминания/задачи/заметки)."""
    days: set[str] = set()
    # заметки: индекс по префиксу даты
    for path in _list_notes(chat_id):
        base = os.path.basename(path)
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_", base)
        if m:
            days.add(m.group(1))
    # напоминания и задачи — по интервалу
    start = datetime(day_from.year, day_from.month, day_from.day, tzinfo=TZ_MSK).astimezone(timezone.utc)
    end = datetime(day_to.year, day_to.month, day_to.day, tzinfo=TZ_MSK).astimezone(timezone.utc)
    # pending напоминания в диапазоне — через get_today по дням дешевле грепнуть всё
    cur = day_from
    while cur <= day_to:
        s, e = _msk_day_to_utc_bounds(cur)
        rems = reminders_db.get_today(chat_id, s, e)
        tks = tasks_db.list_tasks_for_day(chat_id, s, e)
        if rems or tks:
            days.add(cur.isoformat())
        cur = cur + timedelta(days=1)
    return days


def _build_ncal_month_kb(chat_id: int, year: int, month: int):
    now = datetime.now(TZ_MSK)
    busy = _events_for_range(
        chat_id,
        date_cls(year, month, 1),
        date_cls(year, month, cal_stdlib.monthrange(year, month)[1]),
    )
    # строим чуть шире — соседние месяцы на границах не нужны, busy полный по текущему
    rows: list[list[tuple[str, str]]] = []
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    nav = []
    if (prev_y, prev_m) >= (now.year, now.month):
        nav.append(("◀️", f"ncal_m_{prev_y}_{prev_m:02d}"))
    else:
        nav.append(("◀️", "ncal_ignore"))
    nav.append((f"{_MONTHS_CAL[month].capitalize()} {year}", "ncal_ignore"))
    nav.append(("▶️", f"ncal_m_{next_y}_{next_m:02d}"))
    rows.append(nav)
    rows.append([(d, "ncal_ignore") for d in _DAYS_SHORT])
    for week in cal_stdlib.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append((" ", "ncal_ignore"))
                continue
            ds = f"{year}-{month:02d}-{day:02d}"
            label = str(day)
            if ds == now.strftime("%Y-%m-%d"):
                label = f"📍{day}"
            if ds in busy:
                label = f"•{day}" if ds != now.strftime("%Y-%m-%d") else f"📍•{day}"
            row.append((label, f"ncal_d_{ds}"))
        rows.append(row)
    rows.append([("◀️ Меню", "ui:menu")])
    text = (
        f"📅 <b>{_MONTHS_CAL[month].capitalize()} {year}</b>\n"
        "Тапни день — заметки, задачи и напоминания.\n"
        "• — есть события, 📍 — сегодня."
    )
    return text, ui.inline(rows)


def _render_ncal_day(chat_id: int, d: date_cls) -> tuple[str, list[list[tuple[str, str]]]]:
    """Текст дня + inline-кнопки элементов (тап → содержимое)."""
    s, e = _msk_day_to_utc_bounds(d)
    rems = reminders_db.get_today(chat_id, s, e)
    tks = tasks_db.list_tasks_for_day(chat_id, s, e)
    note_paths = _note_paths_on_day(chat_id, d)
    _ncal_day_notes[(chat_id, d.isoformat())] = note_paths
    _ncal_last_day[chat_id] = d.isoformat()
    day_name = _DAYS_SHORT[d.weekday()]
    lines = [f"📅 <b>{d.day} {_month_gen(d.month)} ({day_name})</b>\n"]
    rows: list[list[tuple[str, str]]] = []

    lines.append("⏰ <b>Напоминания</b> — тапни, чтобы открыть")
    if rems:
        for r in rems:
            lines.append(f"• {_fmt_when_msk(r['due_at'])} · {ui.esc(r['text'])}")
            label = f"⏰ {_fmt_when_msk(r['due_at'])} · {r['text']}"
            rows.append([(label[:64], f"ncal_i_r_{r['id']}")])
    else:
        lines.append("— нет")

    lines.append("\n✅ <b>Задачи</b>")
    if tks:
        for t in tks[:8]:
            mark = "☑️" if t["done"] else "•"
            lines.append(f"{mark} {ui.esc(t['text'])}")
            rows.append([(f"{mark} {t['text']}"[:64], f"ncal_i_t_{t['id']}")])
    else:
        lines.append("— нет")

    lines.append("\n📝 <b>Заметки</b>")
    if note_paths:
        for i, path in enumerate(note_paths[:8]):
            title = _note_title(path)
            lines.append(f"• {ui.esc(title)}")
            label = ("📝 " + title)[:64]
            rows.append([(label, f"ncal_i_n_{i}")])
    else:
        lines.append("— нет")

    y, m = d.year, d.month
    rows.append([("◀️ К календарю", f"ncal_m_{y}_{m:02d}"), ("🏠 Меню", "ui:menu")])
    return "\n".join(lines), rows


def _day_back_kb(d: date_cls):
    return ui.inline(
        [[("◀️ К дню", f"ncal_d_{d.isoformat()}"), ("🏠 Меню", "ui:menu")]]
    )


def _day_back_btn(chat_id: int) -> tuple[str, str]:
    if chat_id in _ncal_last_day:
        return ("◀️ К дню", f"ncal_d_{_ncal_last_day[chat_id]}")
    return ("◀️ Календарь", "ncal_ignore")


def _back_row(chat_id: int) -> list[tuple[str, str]]:
    return [_day_back_btn(chat_id), ("🏠 Меню", "ui:menu")]


def _month_gen(m: int) -> str:
    # генитив для «15 октября»
    gen = {
        1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
        7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    return gen[m]


@dp.message(Command("календарь"))
@dp.message(F.text == "📅 Календарь")
async def cmd_calendar(message: types.Message):
    now = datetime.now(TZ_MSK)
    text, kb = _build_ncal_month_kb(message.chat.id, now.year, now.month)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "ncal_ignore")
async def cb_ncal_ignore(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(F.data.startswith("ncal_m_"))
async def cb_ncal_month(call: types.CallbackQuery):
    await call.answer()
    _, _, y, m = call.data.split("_")
    text, kb = _build_ncal_month_kb(call.from_user.id, int(y), int(m))
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("ncal_d_"))
async def cb_ncal_day(call: types.CallbackQuery):
    await call.answer()
    ds = call.data.replace("ncal_d_", "")
    d = datetime.strptime(ds, "%Y-%m-%d").date()
    text, rows = _render_ncal_day(call.from_user.id, d)
    kb = ui.inline(rows)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)


async def _edit_or_send(call: types.CallbackQuery, text: str, kb) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except Exception:
        await call.message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("ncal_i_n_"))
async def cb_ncal_note(call: types.CallbackQuery):
    idx = int(call.data.rsplit("_", 1)[1])
    await call.answer()
    ds = _ncal_last_day.get(call.from_user.id)
    paths = _ncal_day_notes.get((call.from_user.id, ds or ""), [])
    if not paths or idx >= len(paths):
        # fallback: любой кэш этого чата
        for (cid, day), plist in _ncal_day_notes.items():
            if cid == call.from_user.id and idx < len(plist):
                paths, ds = plist, day
                break
    if not paths or idx >= len(paths):
        await call.answer("Заметка не найдена. Открой день заново.", show_alert=True)
        return
    path = paths[idx]
    try:
        with open(path, encoding="utf-8") as f:
            md = f.read()
    except Exception:
        await call.answer("Не удалось прочитать заметку.", show_alert=True)
        return
    html_text = ui.note_to_html(md)
    if len(html_text) > 3800:
        html_text = html_text[:3800] + "\n…"
    text = f"{html_text}\n\n<i>{ui.esc(_note_title(path))}</i>"
    base = os.path.basename(path)
    kb = ui.inline(
        [
            [("📄 Файл", "ncal_i_nf:" + base)],
            [("🗑 Удалить", "ncal_dn:" + base)],
            _back_row(call.from_user.id),
        ]
    )
    await _edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("ncal_i_nf:"))
async def cb_ncal_note_file(call: types.CallbackQuery):
    base = call.data.split(":", 1)[1]
    await call.answer("Отправляю файл")
    path = _safe_user_note_path(call.from_user.id, base)
    if os.path.isfile(path):
        await call.message.answer_document(
            types.FSInputFile(path, filename=os.path.basename(path))
        )


@dp.callback_query(F.data.startswith("ncal_i_t_"))
async def cb_ncal_task(call: types.CallbackQuery):
    tid = int(call.data.rsplit("_", 1)[1])
    await call.answer()
    t = tasks_db.get_task(tid, call.from_user.id)
    if not t:
        await call.answer("Задача не найдена.", show_alert=True)
        return
    created = (t["created_at"] or "")[:16].replace("T", " ")
    done_at = (t["done_at"] or "")[:16].replace("T", " ") if t["done_at"] else None
    status = "✅ Готово" if t["done"] else "🔓 В работе"
    text = (
        f"✅ <b>Задача #{t['id']}</b>\n\n"
        f"{ui.esc(t['text'])}\n\n"
        f"Статус: {status}\n"
        f"Создана: {created} UTC"
    )
    if done_at:
        text += f"\nЗакрыта: {done_at} UTC"
    kb = ui.inline(
        [
            [
                ("↩️ Вернуть", f"ui:tasks:undone:{tid}")
                if t["done"]
                else ("✔️ Готово", f"ui:tasks:done:{tid}")
            ],
            [("✏️ Изменить", f"ncal_et_{tid}"), ("🗑 Удалить", f"ncal_dt_{tid}")],
            _back_row(call.from_user.id),
        ]
    )
    await _edit_or_send(call, text, kb)


@dp.callback_query(F.data.startswith("ncal_i_r_"))
async def cb_ncal_reminder(call: types.CallbackQuery):
    rid = int(call.data.rsplit("_", 1)[1])
    await call.answer()
    r = reminders_db.get_reminder(rid, call.from_user.id)
    if not r:
        await call.answer("Напоминание не найдено.", show_alert=True)
        return
    text = (
        f"⏰ <b>Напоминание #{r['id']}</b>\n\n"
        f"📌 {ui.esc(r['text'])}\n\n"
        f"🗓 {_fmt_when_msk(r['due_at'])}{_repeat_label(r['repeat_rule'])}\n"
        f"Статус: {r['status']}"
    )
    kb = ui.inline(
        [
            [("❌ Отмена", f"ui:rem:cancel:{rid}"), ("✅ Готово", f"ui:rem:done:{rid}")],
            [("✏️ Изменить", f"ncal_er_{rid}")],
            _back_row(call.from_user.id),
        ]
    )
    await _edit_or_send(call, text, kb)


async def _return_to_day(call: types.CallbackQuery) -> None:
    ds = _ncal_last_day.get(call.from_user.id)
    if not ds:
        await _edit_or_send(call, "📅 Календарь.", ui.inline([[("🏠 Меню", "ui:menu")]]))
        return
    d = datetime.strptime(ds, "%Y-%m-%d").date()
    text, rows = _render_ncal_day(call.from_user.id, d)
    await _edit_or_send(call, text, ui.inline(rows))


async def _return_to_day_by_chat(chat_id: int, message: types.Message) -> None:
    """После правки текста — снова открыть день (новым сообщением)."""
    ds = _ncal_last_day.get(chat_id)
    if not ds:
        return
    d = datetime.strptime(ds, "%Y-%m-%d").date()
    text, rows = _render_ncal_day(chat_id, d)
    await message.answer(text, reply_markup=ui.inline(rows))


# --- редактирование текста (последующее сообщение) ---

@dp.callback_query(F.data.startswith("ncal_et_"))
async def cb_edit_task_prompt(call: types.CallbackQuery):
    tid = int(call.data.rsplit("_", 1)[1])
    _edit_pending[call.from_user.id] = {"kind": "task", "id": tid}
    await call.answer()
    await call.message.answer(
        f"✏️ Пришли новый текст для задачи #{tid}.\nОтмена — /отмена"
    )


@dp.callback_query(F.data.startswith("ncal_er_"))
async def cb_edit_rem_prompt(call: types.CallbackQuery):
    rid = int(call.data.rsplit("_", 1)[1])
    _edit_pending[call.from_user.id] = {"kind": "rem", "id": rid}
    await call.answer()
    await call.message.answer(
        f"✏️ Пришли новый текст напоминания #{rid} (время не изменится).\nОтмена — /отмена"
    )


@dp.message(Command("отмена"))
async def cmd_edit_cancel(message: types.Message):
    if _edit_pending.pop(message.chat.id, None):
        await message.answer("✏️ Отменено.")
    else:
        await message.answer("Нечего отменять.")


# --- удаление с подтверждением ---

@dp.callback_query(F.data.startswith("ncal_dt_"))
async def cb_del_task_confirm(call: types.CallbackQuery):
    tid = int(call.data.rsplit("_", 1)[1])
    await call.answer()
    kb = ui.inline(
        [
            [("Да, удалить", f"ncal_dty_{tid}"), ("Нет", f"ncal_i_t_{tid}")],
            _back_row(call.from_user.id),
        ]
    )
    await _edit_or_send(call, f"🗑 Удалить задачу #{tid}?", kb)


@dp.callback_query(F.data.startswith("ncal_dty_"))
async def cb_del_task_yes(call: types.CallbackQuery):
    tid = int(call.data.rsplit("_", 1)[1])
    await call.answer()
    tasks_db.delete_task(tid, call.from_user.id)
    await _return_to_day(call)


@dp.callback_query(F.data.startswith("ncal_dn:"))
async def cb_del_note_confirm(call: types.CallbackQuery):
    base = call.data.split(":", 1)[1]
    safe = os.path.basename(base)
    await call.answer()
    kb = ui.inline(
        [
            [("Да, удалить", f"ncal_dny:{safe}"), ("Нет", "ncal_ignore")],
            _back_row(call.from_user.id),
        ]
    )
    await _edit_or_send(call, f"🗑 Удалить заметку «{ui.esc(safe)}»?", kb)


@dp.callback_query(F.data.startswith("ncal_dny:"))
async def cb_del_note_yes(call: types.CallbackQuery):
    safe = os.path.basename(call.data.split(":", 1)[1])
    await call.answer()
    path = _safe_user_note_path(call.from_user.id, safe)
    if os.path.isfile(path):
        os.remove(path)
        # почистить кэш дня
        for key in list(_ncal_day_notes):
            if call.from_user.id == key[0]:
                _ncal_day_notes[key] = [p for p in _ncal_day_notes[key] if p != path]
    await _return_to_day(call)


# ---------- CALLBACK-МЕНЮ ----------

@dp.callback_query(F.data == "ui:noop")
async def cb_noop(call: types.CallbackQuery):
    await call.answer()


@dp.callback_query(F.data == "ui:menu")
async def cb_menu(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer(
        ui.card("Личный ассистент", "Разделы:", "🤖"),
        reply_markup=ui.main_kb(),
    )


# --- заметки ---

@dp.callback_query(F.data == "ui:notes:last")
async def cb_notes_last(call: types.CallbackQuery):
    await call.answer()
    files = _list_notes(call.from_user.id)
    if not files:
        await call.message.answer("Заметок пока нет. Пришли голосовое 🎙")
        return
    idx = 0
    await _send_note_page(call.message, files, idx)


@dp.callback_query(F.data.startswith("ui:notes:prev:"))
@dp.callback_query(F.data.startswith("ui:notes:next:"))
async def cb_notes_page(call: types.CallbackQuery):
    idx = int(call.data.rsplit(":", 1)[1])
    await call.answer()
    files = _list_notes(call.from_user.id)
    if not files:
        await call.message.answer("Заметок больше нет.")
        return
    idx = max(0, min(idx, len(files) - 1))
    await _send_note_page(call.message, files, idx)


async def _send_note_page(message: types.Message, files: list[str], idx: int):
    path = files[idx]
    try:
        with open(path, encoding="utf-8") as f:
            md = f.read()
    except Exception:
        await message.answer("Не удалось прочитать заметку.")
        return
    html_text = ui.note_to_html(md)
    if len(html_text) > 4000:
        html_text = html_text[:4000] + "\n…"
    await message.answer(
        f"{html_text}\n\n<i>{ui.esc(_note_title(path))}</i>",
        reply_markup=ui.note_pager_kb(idx, len(files)),
    )


@dp.callback_query(F.data.startswith("ui:notes:file:"))
async def cb_notes_file(call: types.CallbackQuery):
    idx = int(call.data.rsplit(":", 1)[1])
    await call.answer("Отправляю файл")
    files = _list_notes(call.from_user.id)
    if 0 <= idx < len(files):
        await call.message.answer_document(
            types.FSInputFile(files[idx], filename=os.path.basename(files[idx]))
        )


@dp.callback_query(F.data == "ui:notes:search")
async def cb_notes_search(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer(
        "🔍 Поиск по заметкам:\n<code>/find слово</code>\n\n"
        "Просто напиши боту любое сообщение — я отвечу как ассистент."
    )


# --- напоминания ---

async def _send_reminders(message: types.Message, rows, empty_text: str):
    if not rows:
        await message.answer(
            empty_text,
            reply_markup=ui.inline(
                [[("ℹ️ Как создать", "ui:rem:help"), ("🏠 Меню", "ui:menu")]]
            ),
        )
        return
    await message.answer(f"⏰ <b>Напоминания ({len(rows)})</b>")
    for r in rows:
        today = " ⏳ сегодня" if _is_today_msk(r["due_at"]) else ""
        await message.answer(
            f"#{r['id']} · {_fmt_when_msk(r['due_at'])}{today}{_repeat_label(r['repeat_rule'])}\n"
            f"📌 {ui.esc(r['text'])}",
            reply_markup=ui.reminder_item_kb(r["id"]),
        )


@dp.callback_query(F.data == "ui:rem:today")
async def cb_rem_today(call: types.CallbackQuery):
    await call.answer()
    now = datetime.now(TZ_MSK)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    rows = reminders_db.get_today(call.from_user.id, start, end)
    await _send_reminders(call.message, rows, "На сегодня напоминаний нет 🎉")


@dp.callback_query(F.data == "ui:rem:all")
async def cb_rem_all(call: types.CallbackQuery):
    await call.answer()
    rows = reminders_db.list_pending(call.from_user.id)
    await _send_reminders(call.message, rows, "Активных напоминаний нет.")


@dp.callback_query(F.data == "ui:rem:help")
async def cb_rem_help(call: types.CallbackQuery):
    await call.answer()
    await call.message.answer(
        "ℹ️ <b>Как создать напоминание</b>\n\n"
        "Голосом или текстом:\n"
        "«напомни завтра в 15:00 позвонить врачу»\n"
        "«напомни через 2 часа вынести мусор»\n"
        "«каждый день в 9:30 проверить почту» 🔁\n"
        "«каждую среду в 18:00 тренировка» 🔁"
    )


@dp.callback_query(F.data.startswith("ui:rem:cancel:"))
async def cb_rem_cancel(call: types.CallbackQuery):
    rid = int(call.data.rsplit(":", 1)[1])
    ok = reminders_db.cancel(rid, call.from_user.id)
    await call.answer("Отменено ✅" if ok else "Не найдено")
    if ok:
        await call.message.answer(f"🗑 Напоминание #{rid} отменено.")


@dp.callback_query(F.data.startswith("ui:rem:done:"))
async def cb_rem_done(call: types.CallbackQuery):
    rid = int(call.data.rsplit(":", 1)[1])
    reminders_db.complete(rid)
    await call.answer("Готово ✅")
    try:
        await call.message.edit_text(f"✅ <b>Готово</b> — напоминание #{rid} закрыто.")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ui:rem:snooze:"))
async def cb_rem_snooze(call: types.CallbackQuery):
    parts = call.data.split(":")  # ui:rem:snooze:<id>:<sec>
    rid, sec = int(parts[3]), int(parts[4])
    new_due = datetime.now(timezone.utc) + timedelta(seconds=sec)
    ok = reminders_db.snooze(rid, new_due)
    await call.answer("Перенёс ⏰" if ok else "Не найдено")
    if ok:
        label = "через 1 час" if sec == 3600 else "на завтра"
        try:
            await call.message.edit_text(f"⏰ Напоминание #{rid} перенесено {label}.")
        except Exception:
            pass


# --- задачи ---

@dp.callback_query(F.data == "ui:tasks:active")
async def cb_tasks_active(call: types.CallbackQuery):
    await call.answer()
    rows = tasks_db.list_tasks(call.from_user.id, done=False)
    if not rows:
        await call.message.answer(
            "Активных задач нет. Создай: «задача: ...»",
            reply_markup=ui.inline([[("🏠 Меню", "ui:menu")]]),
        )
        return
    for t in rows:
        await call.message.answer(
            f"📌 #{t['id']} {ui.esc(t['text'])}",
            reply_markup=ui.task_item_kb(t["id"], done=False),
        )


@dp.callback_query(F.data == "ui:tasks:done")
async def cb_tasks_done(call: types.CallbackQuery):
    await call.answer()
    rows = tasks_db.list_tasks(call.from_user.id, done=True)
    if not rows:
        await call.message.answer(
            "Сделанных задач пока нет.",
            reply_markup=ui.inline([[("🏠 Меню", "ui:menu")]]),
        )
        return
    for t in rows:
        await call.message.answer(
            f"✅ #{t['id']} {ui.esc(t['text'])}",
            reply_markup=ui.task_item_kb(t["id"], done=True),
        )


@dp.callback_query(F.data.startswith("ui:tasks:done:"))
async def cb_task_done(call: types.CallbackQuery):
    tid = int(call.data.rsplit(":", 1)[1])
    tasks_db.set_done(tid, call.from_user.id, True)
    await call.answer("Готово ✅")
    try:
        await call.message.edit_text(f"✅ Задача #{tid} выполнена.")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ui:tasks:undone:"))
async def cb_task_undone(call: types.CallbackQuery):
    tid = int(call.data.rsplit(":", 1)[1])
    tasks_db.set_done(tid, call.from_user.id, False)
    await call.answer("Вернул")
    try:
        await call.message.edit_text(f"↩️ Задача #{tid} снова активна.")
    except Exception:
        pass


# --- финансы ---

@dp.callback_query(F.data == "ui:fin:balance")
async def cb_fin_balance(call: types.CallbackQuery):
    await call.answer()
    month = datetime.now(TZ_MSK).strftime("%Y-%m")
    s = finance_db.month_summary(call.from_user.id, month)
    await call.message.answer(
        f"💰 <b>Баланс · {_month_label(month)}</b>\n\n"
        f"Доходы:   <b>+{s['income']:.0f} ₽</b>\n"
        f"Расходы:  <b>−{s['expense']:.0f} ₽</b>\n"
        f"Остаток:  <b>{_money(s['balance'])}</b>"
    )


@dp.callback_query(F.data == "ui:fin:report")
async def cb_fin_report(call: types.CallbackQuery):
    await call.answer()
    month = datetime.now(TZ_MSK).strftime("%Y-%m")
    rows = finance_db.month_by_category(call.from_user.id, month)
    if not rows:
        await call.message.answer(
            f"За {_month_label(month)} операций пока нет.",
            reply_markup=ui.inline([[("🏠 Меню", "ui:menu")]]),
        )
        return
    lines = [f"📊 <b>Категории · {_month_label(month)}</b>\n"]
    for r in rows:
        if r["expense"] > 0:
            lines.append(f"• {ui.esc(r['category'])} — <b>−{r['expense']:.0f} ₽</b>")
        if r["income"] > 0:
            lines.append(f"• {ui.esc(r['category'])} — <b>+{r['income']:.0f} ₽</b>")
    await call.message.answer("\n".join(lines))


@dp.callback_query(F.data == "ui:fin:list")
async def cb_fin_list(call: types.CallbackQuery):
    await call.answer()
    rows = finance_db.recent(call.from_user.id, 10)
    if not rows:
        await call.message.answer(
            "Операций пока нет.",
            reply_markup=ui.inline([[("🏠 Меню", "ui:menu")]]),
        )
        return
    lines = ["🧾 <b>Последние операции</b>\n"]
    for r in rows:
        comment = f" — {ui.esc(r['comment'])}" if r["comment"] else ""
        lines.append(f"#{r['id']} {_date_short(r['occurred_on'])} · {_money(r['amount'])} · {ui.esc(r['category'])}{comment}")
    await call.message.answer("\n".join(lines))


# ---------- обработка голосовых ----------

def _is_conversational(transcript: str) -> bool:
    """Короткая реплика или вопрос → голос уходит в чат, не в заметку."""
    t = " ".join(transcript.split())
    if not t:
        return False
    if "?" in t:
        return True
    if len(t) <= 120:
        return True
    return bool(
        re.match(
            r"^(что|почему|зачем|как|когда|где|кто|сколько|расскажи|скажи|можешь|умеешь)\b",
            t,
            re.IGNORECASE,
        )
    )


async def _try_auto_fact(chat_id: int, user_text: str) -> None:
    """Тихо запомнить факт из реплики (source=auto), без «запомни»."""
    t = user_text.strip()
    if not t or len(t) < 8 or len(t) > 300:
        return
    # «запомни …» уже обработано вручную — не дублируем
    if re.match(r"^запомни\b", t, re.IGNORECASE):
        return
    try:
        fact = await ai_service.extract_fact(t)
        if fact and len(fact.strip()) >= 6:
            memory.add_fact(chat_id, fact.strip(), source="auto")
    except Exception as e:
        logger.warning(f"Авто-факт не извлечён: {e}")


async def _assistant_reply(message: types.Message, user_text: str) -> None:
    """Голос/текст → свободный чат с агентом (инструменты + скиллы + история)."""
    memory.add_dialog(message.chat.id, "user", user_text)
    try:
        context = await _build_context(message.chat.id, user_text)
        history = memory.get_history(message.chat.id, limit=12)
        # текущая реплика уже в history; chat() добавит её ещё раз как user —
        # уберём последний user-дубль из history
        if history and history[-1].get("role") == "user":
            history = history[:-1]
        reply = await ai_service.chat(
            user_text,
            context,
            execute_tool=_make_tool_executor(message.chat.id),
            skills=skills.load_skills(),
            history=history,
            user_name=message.from_user.first_name if message.from_user else "",
        )
        memory.add_dialog(message.chat.id, "assistant", reply)
        await deliver(message, ui.esc(reply))
        asyncio.create_task(_try_auto_fact(message.chat.id, user_text))
    except Exception as e:
        logger.error(f"Ошибка чата (голос): {e}")
        await message.answer("❌ Не получилось ответить. Попробуй ещё раз.")


async def process_local_audio_file(
    message: types.Message, local_path: str, display_name: str, status_msg: types.Message
):
    mp3_path = None
    try:
        try:
            await status_msg.edit_text("🎛 Оптимизирую аудио…")
        except Exception:
            pass

        mp3_path = AudioService.convert_to_mp3(local_path)
        if local_path != mp3_path and os.path.exists(local_path):
            os.remove(local_path)

        audio_chunks = AudioService.chunk_audio_if_needed(mp3_path)

        results = []
        for i, chunk in enumerate(audio_chunks):
            if i > 0:
                await asyncio.sleep(2)
            try:
                results.append(await transcribe_service.transcribe_file(chunk))
            except Exception as e:
                logger.error(f"Транскрипция чанка {i} не удалась: {e}")
                results.append(f"\n[Ошибка распознавания части {i + 1}]\n")
            if os.path.exists(chunk):
                os.remove(chunk)

        full_transcript = "\n\n".join(results)
        if not full_transcript.strip():
            await status_msg.edit_text("❌ Не удалось распознать речь.")
            return

        extras: list[str] = []

        # запомни … → факт (как в текстовом чате)
        m_fact = re.match(r"^запомни(?:\s+что)?[,:]?\s+(.+)", full_transcript, re.IGNORECASE)
        if m_fact:
            tail = m_fact.group(1).strip()
            try:
                fact = await ai_service.extract_fact(tail) or tail
            except Exception:
                fact = tail
            memory.add_fact(message.chat.id, fact)
            await deliver(message, f"🧠 <b>Записал:</b> {ui.esc(fact)}")
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        # напоминание
        if re.search(r"напомни", full_transcript, re.IGNORECASE):
            parsed = await ai_service.parse_reminder(full_transcript, datetime.now(TZ_MSK))
            if parsed:
                rid = reminders_db.add_reminder(
                    message.chat.id, parsed["text"], parsed["when"], parsed.get("repeat")
                )
                extras.append(
                    f"⏰ Напоминание #{rid}: <b>{ui.esc(parsed['text'])}</b>\n"
                    f"🗓 {_fmt_when_msk(parsed['when'].astimezone(timezone.utc).isoformat())}"
                    f"{_repeat_label(parsed.get('repeat'))}"
                )

        # задача
        if re.search(r"задач|не забыть|todo", full_transcript, re.IGNORECASE):
            task_text = await ai_service.parse_task(full_transcript)
            if task_text:
                tid = tasks_db.add_task(message.chat.id, task_text)
                extras.append(f"✅ Задача #{tid}: <b>{ui.esc(task_text)}</b>")

        # финансы
        if re.search(r"\d", full_transcript):
            fin = await ai_service.parse_finance(
                full_transcript, datetime.now(TZ_MSK).strftime("%Y-%m-%d")
            )
            if fin:
                finance_db.add_operation(
                    message.chat.id, fin["amount"], fin["category"], fin["comment"], fin["date"]
                )
                extras.append(f"💸 Записал: <b>{_money(fin['amount'])}</b> · {ui.esc(fin['category'])}")

        # короткая реплика / вопрос → диалог, не заметка
        if _is_conversational(full_transcript):
            try:
                await status_msg.edit_text("🤖 Думаю…")
            except Exception:
                pass
            for ex in extras:
                await deliver(message, ex)
            await _assistant_reply(message, full_transcript)
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        # заметка
        try:
            await status_msg.edit_text("🤖 Читаю и чищу текст…")
        except Exception:
            pass
        try:
            markdown_report = await ai_service.structure_note(full_transcript)
        except Exception as e:
            logger.error(f"Ошибка LLM: {e}")
            await status_msg.edit_text("❌ Ошибка анализа текста. Попробуй ещё раз.")
            return

        if not markdown_report or not markdown_report.strip():
            await status_msg.edit_text("⚠️ Заметка получилась пустой.")
            return

        note_path = _save_note(markdown_report, display_name, message.chat.id)

        body = ui.note_to_html(markdown_report)
        if len(body) > 3500:
            body = body[:3500] + "\n…"
        try:
            if extras and kv.voice_enabled(message.chat.id):
                # голос включён: заметка текстом, подтверждения отдельно (текст+голос)
                await message.answer(body)
                for ex in extras:
                    await deliver(message, ex)
            else:
                if extras:
                    body += "\n\n" + "\n\n".join(extras)
                await message.answer(body)
        except Exception as e:
            logger.error(f"Не удалось отправить заметку: {e}")

        try:
            await message.answer_document(
                types.FSInputFile(note_path, filename=os.path.basename(note_path)),
                caption="📄 Файл заметки",
            )
        except Exception as e:
            logger.error(f"Не удалось отправить файл: {e}")

        # живой ответ собеседника к длинной заметке (1–2 фразы)
        try:
            ack = await ai_service.chat(
                "Пользователь наголосил заметку. Ответь ему живо, 1–2 предложения, "
                "как внимательный собеседник — без канцелярита:\n\n" + full_transcript[:2000],
                skills=skills.load_skills(),
                user_name=message.from_user.first_name if message.from_user else "",
            )
            if ack:
                await deliver(message, ui.esc(ack))
        except Exception as e:
            logger.warning(f"Короткий ответ к заметке не удался: {e}")

        # если в заметке было время — предложить напоминание кнопкой
        rem_kb = None
        try:
            parsed = await ai_service.parse_reminder(full_transcript, datetime.now(TZ_MSK))
            if parsed and parsed.get("when"):
                _pending_note_rem[message.chat.id] = parsed
                rem_kb = ui.inline(
                    [[("⏰ Создать напоминание", "ui:note:rem")]]
                )
        except Exception as e:
            logger.warning(f"parse_reminder для заметки не удался: {e}")
        if rem_kb is not None:
            try:
                await message.answer(
                    "Похоже, в заметке есть время — создать напоминание?",
                    reply_markup=rem_kb,
                )
            except Exception:
                pass

        try:
            await status_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Ошибка обработки аудио: {e}")
        try:
            await status_msg.edit_text("❌ Произошла ошибка при обработке.")
        except Exception:
            pass
    finally:
        if mp3_path and os.path.exists(mp3_path):
            os.remove(mp3_path)
        if os.path.exists(local_path):
            os.remove(local_path)


async def process_audio_message(
    message: types.Message, file_id: str, file_name: str, file_size: Optional[int] = None
):
    MAX_TELEGRAM_DOWNLOAD_SIZE = 20 * 1024 * 1024
    if file_size and file_size > MAX_TELEGRAM_DOWNLOAD_SIZE:
        await message.answer("⚠️ Файл больше 20 MB — Telegram не даст скачать. Разбей или пришли ссылкой.")
        return

    status_msg = await message.answer("📥 Скачиваю аудио…")
    orig_ext = os.path.splitext(file_name)[1].lower()
    if not orig_ext or len(orig_ext) > 5:
        orig_ext = ".ogg"
    safe_name = f"file_{file_id[-10:]}{orig_ext}"
    local_path = os.path.join(DOWNLOADS_DIR, safe_name)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    try:
        file_info = await bot.get_file(file_id)
        await bot.download_file(file_info.file_path, local_path)
        await process_local_audio_file(message, local_path, file_name, status_msg)
    except Exception as e:
        logger.error(f"Не удалось скачать файл: {e}")
        await status_msg.edit_text("❌ Не удалось скачать аудио из Telegram.")
        if os.path.exists(local_path):
            os.remove(local_path)


@dp.message(F.voice)
async def voice_handler(message: types.Message):
    await process_audio_message(
        message, message.voice.file_id, f"заметка_{message.voice.file_unique_id}.ogg", message.voice.file_size
    )


@dp.message(F.audio)
async def audio_handler(message: types.Message):
    orig = message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
    await process_audio_message(message, message.audio.file_id, orig, message.audio.file_size)


@dp.message(F.document & F.document.mime_type.startswith("audio/"))
async def audio_document_handler(message: types.Message):
    orig = message.document.file_name or f"doc_{message.document.file_unique_id}.mp3"
    await process_audio_message(message, message.document.file_id, orig, message.document.file_size)


# ---------- команды-алиасы ----------

@dp.message(Command("last"))
async def cmd_last(message: types.Message):
    parts = (message.text or "").split()
    files = _list_notes(message.chat.id)
    if len(parts) > 1 and parts[1].isdigit():
        n = max(1, min(int(parts[1]), 20))
        files = files[:n]
    if not files:
        await message.answer("Заметок пока нет.")
        return
    for path in reversed(files):
        await message.answer_document(types.FSInputFile(path, filename=os.path.basename(path)))


@dp.message(Command("find"))
async def cmd_find(message: types.Message):
    query = (message.text or "").strip().partition(" ")[2].strip()
    await _do_search(message, query)


async def _do_search(message: types.Message, query: str):
    if not query:
        await message.answer("Напиши слово для поиска.")
        return
    q = query.lower()
    matches = []
    for path in _list_notes(message.chat.id):
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read().lower()
        except Exception:
            continue
        if q in os.path.basename(path).lower() or q in content:
            matches.append(path)
        if len(matches) >= 10:
            break
    if not matches:
        await message.answer(f"Ничего не нашлось по «{ui.esc(query)}».")
        return
    await message.answer(f"🔍 Найдено заметок: {len(matches)}")
    for path in matches:
        await message.answer_document(types.FSInputFile(path, filename=os.path.basename(path)))


@dp.callback_query(F.data == "ui:note:rem")
async def cb_note_rem(call: types.CallbackQuery):
    pending = _pending_note_rem.pop(call.from_user.id, None)
    if not pending:
        await call.answer("Это напоминание уже неактуально.", show_alert=True)
        return
    rid = reminders_db.add_reminder(
        call.from_user.id,
        pending["text"],
        pending["when"],
        pending.get("repeat"),
    )
    when = pending["when"].astimezone(timezone.utc).isoformat()
    await call.answer(f"Напоминание #{rid} создано ✅", show_alert=False)
    await call.message.answer(
        f"⏰ <b>Напоминание #{rid}</b>\n📌 {ui.esc(pending['text'])}\n"
        f"🗓 {_fmt_when_msk(when)}{_repeat_label(pending.get('repeat'))}",
        reply_markup=ui.reminder_item_kb(rid),
    )


@dp.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    rows = reminders_db.list_pending(message.chat.id)
    await _send_reminders(message, rows, "Активных напоминаний нет.")


@dp.message(Command("завтра"))
async def cmd_tomorrow(message: types.Message):
    """Быстрый список на завтра без LLM."""
    now = datetime.now(TZ_MSK)
    day_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = day_start.astimezone(timezone.utc)
    end = (day_start + timedelta(days=1)).astimezone(timezone.utc)
    rems = reminders_db.get_today(message.chat.id, start, end)
    tasks = tasks_db.list_tasks(message.chat.id, done=False)
    lines = [f"📆 <b>Завтра ({(now + timedelta(days=1)).strftime('%d.%m')})</b>\n"]
    if rems:
        lines.append(f"⏰ Напоминаний: <b>{len(rems)}</b>")
        for r in rems[:8]:
            lines.append(f"• {ui.esc(r['text'])} — {_fmt_when_msk(r['due_at'])}")
    else:
        lines.append("⏰ Напоминаний нет")
    if tasks:
        lines.append(f"\n✅ Активных задач: <b>{len(tasks)}</b>")
        for t in tasks[:5]:
            lines.append(f"• {ui.esc(t['text'])}")
    await message.answer("\n".join(lines))


@dp.message(Command("users"))
async def cmd_users(message: types.Message):
    """Список кто заходил — только владелец (ALLOWED_IDS)."""
    if ALLOWED_IDS and message.from_user.id not in ALLOWED_IDS:
        return  # молча
    try:
        memory.import_users_from_dialog()
    except Exception as e:
        logger.warning(f"import_users_from_dialog: {e}")
    rows = memory.list_users(80)
    if not rows:
        await message.answer("Пока никто не заходил.")
        return
    lines = [f"👥 <b>Заходили в бота ({len(rows)})</b>\n"]
    for r in rows:
        name = r["first_name"] or "—"
        uname = f"@{r['username']}" if r["username"] else ""
        last = (r["last_seen"] or "")[:16].replace("T", " ")
        lines.append(f"• {ui.esc(name)} {uname} · <code>{r['user_id']}</code> · {last} UTC")
    await message.answer("\n".join(lines[:60]))


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /cancel <id>")
        return
    if reminders_db.cancel(int(parts[1]), message.chat.id):
        await message.answer(f"🗑 Напоминание #{parts[1]} отменено.")
    else:
        await message.answer(f"Напоминание #{parts[1]} не найдено.")


@dp.message(Command("баланс"))
@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    month = datetime.now(TZ_MSK).strftime("%Y-%m")
    s = finance_db.month_summary(message.chat.id, month)
    await message.answer(
        f"💰 <b>Баланс · {_month_label(month)}</b>\n\n"
        f"Доходы:   <b>+{s['income']:.0f} ₽</b>\n"
        f"Расходы:  <b>−{s['expense']:.0f} ₽</b>\n"
        f"Остаток:  <b>{_money(s['balance'])}</b>"
    )


@dp.message(Command("отчёт"))
@dp.message(Command("report"))
async def cmd_report(message: types.Message):
    month = datetime.now(TZ_MSK).strftime("%Y-%m")
    rows = finance_db.month_by_category(message.chat.id, month)
    if not rows:
        await message.answer(
            f"За {_month_label(month)} операций пока нет.",
            reply_markup=ui.inline([[("🏠 Меню", "ui:menu")]]),
        )
        return
    lines = [f"📊 <b>Категории · {_month_label(month)}</b>\n"]
    for r in rows:
        if r["expense"] > 0:
            lines.append(f"• {ui.esc(r['category'])} — <b>−{r['expense']:.0f} ₽</b>")
    await message.answer("\n".join(lines))


@dp.message(Command("расходы"))
@dp.message(Command("expenses"))
async def cmd_expenses(message: types.Message):
    parts = (message.text or "").split()
    limit = 10
    if len(parts) > 1 and parts[1].isdigit():
        limit = max(1, min(int(parts[1]), 30))
    rows = finance_db.recent(message.chat.id, limit)
    if not rows:
        await message.answer(
            "Операций пока нет.",
            reply_markup=ui.inline([[("🏠 Меню", "ui:menu")]]),
        )
        return
    lines = [f"🧾 <b>Операции</b>\n"]
    for r in rows:
        lines.append(f"#{r['id']} {_date_short(r['occurred_on'])} · {_money(r['amount'])} · {ui.esc(r['category'])}")
    await message.answer("\n".join(lines))


async def _build_context(chat_id: int, query: str = "") -> str:
    """Контекст для чата: факты, задачи, напоминания, заметки, история диалога."""
    parts: list[str] = []

    facts = memory.facts_context(chat_id)
    if facts:
        parts.append(facts)

    tasks_rows = tasks_db.list_tasks(chat_id, done=False)
    if tasks_rows:
        parts.append("Активные задачи: " + "; ".join(t["text"] for t in tasks_rows[:5]))

    now = datetime.now(TZ_MSK)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    rems = reminders_db.get_today(chat_id, start, start + timedelta(days=1))
    if rems:
        parts.append("Напоминания на сегодня: " + "; ".join(r["text"] for r in rems[:5]))

    # заметки: релевантные по запросу + всегда немного последних (контекст «про него»)
    note_lines: list[str] = []
    seen: set[str] = set()
    user_notes = _user_notes_dir(chat_id)
    if query:
        for hit in memory.search_notes(user_notes, query, limit=3):
            p = hit["path"]
            if p in seen:
                continue
            seen.add(p)
            note_lines.append(f"- {os.path.basename(p)}: {hit['snippet'][:350]}")
    for path in _list_notes(chat_id)[:4]:
        if path in seen:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                note_lines.append(
                    f"- {os.path.basename(path)}: {f.read()[:350].replace(chr(10), ' ').strip()}"
                )
            seen.add(path)
        except Exception:
            continue
    if note_lines:
        parts.append("Заметки:\n" + "\n".join(note_lines[:6]))

    # история диалога передаётся ролями в chat(history=...), сюда не дублируем
    return "\n".join(parts)


# ---------- текст: напоминание / задача / финансы / чат ----------

def _make_tool_executor(chat_id: int):
    """Возвращает async-диспетчер инструментов агента, привязанный к чату."""

    async def execute_tool(name: str, args: dict) -> str:
        if name == "web_search":
            return await tools_search.web_search(str(args.get("query", "")))
        if name == "search_notes":
            hits = memory.search_notes(
                _user_notes_dir(chat_id), str(args.get("query", "")), limit=4
            )
            if not hits:
                return "В заметках ничего не нашлось."
            return "\n".join(
                f"- {os.path.basename(h['path'])}: {h['snippet'][:300]}" for h in hits
            )
        if name == "get_balance":
            month = datetime.now(TZ_MSK).strftime("%Y-%m")
            s = finance_db.month_summary(chat_id, month)
            return (
                f"Баланс за {month}: доходы {_money(s['income'])}, "
                f"расходы {_money(s['expense'])}, остаток {_money(s['balance'])}"
            )
        return f"Неизвестный инструмент: {name}"

    return execute_tool


@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return

    # ожидание нового текста задачи/напоминания (✏️ Изменить в календаре)
    pending = _edit_pending.pop(message.chat.id, None)
    if pending:
        if pending["kind"] == "task":
            ok = tasks_db.set_text(pending["id"], message.chat.id, text)
            msg = f"✅ Задача #{pending['id']} обновлена." if ok else "❌ Задача не найдена."
        else:
            ok = reminders_db.set_text(pending["id"], message.chat.id, text)
            msg = f"✅ Напоминание #{pending['id']} обновлено." if ok else "❌ Напоминание не найдено."
        await message.answer(msg)
        await _return_to_day_by_chat(message.chat.id, message)
        return

    # --- память: «запомни ...» → факт; «что ты помнишь?» → список ---
    m = re.match(r"^запомни(?:\s+что)?[,:]?\s+(.+)", text, re.IGNORECASE)
    if m:
        tail = m.group(1).strip()
        try:
            fact = await ai_service.extract_fact(tail) or tail
        except Exception:
            fact = tail
        memory.add_fact(message.chat.id, fact)
        await deliver(message, f"🧠 <b>Записал:</b> {ui.esc(fact)}")
        return

    if re.search(r"что (ты )?помнишь|что ты обо мне знаешь|мои факты", text, re.IGNORECASE):
        facts = memory.list_facts(message.chat.id)
        if facts:
            body = "\n".join(f"• {ui.esc(f)}" for f in facts[:30])
            await deliver(message, f"🧠 <b>Что помню ({len(facts)}):</b>\n{body}")
        else:
            await deliver(message, "Пока ничего не помню. Скажи, например, «запомни, что я живу в Москве».")
        return

    if re.search(r"напомни", text, re.IGNORECASE):
        parsed = await ai_service.parse_reminder(text, datetime.now(TZ_MSK))
        if parsed:
            rid = reminders_db.add_reminder(
                message.chat.id, parsed["text"], parsed["when"], parsed.get("repeat")
            )
            await deliver(
                message,
                f"⏰ <b>Напоминание #{rid}</b>\n"
                f"📌 {ui.esc(parsed['text'])}\n"
                f"🗓 {_fmt_when_msk(parsed['when'].astimezone(timezone.utc).isoformat())}"
                f"{_repeat_label(parsed.get('repeat'))}",
                reply_markup=ui.reminder_item_kb(rid),
            )
            return

    if re.search(r"задач|не забыть|todo", text, re.IGNORECASE):
        task_text = await ai_service.parse_task(text)
        if task_text:
            tid = tasks_db.add_task(message.chat.id, task_text)
            await deliver(
                message,
                f"✅ <b>Задача #{tid}</b>\n📌 {ui.esc(task_text)}",
                reply_markup=ui.task_item_kb(tid, done=False),
            )
            return

    if re.search(r"\d", text):
        fin = await ai_service.parse_finance(text, datetime.now(TZ_MSK).strftime("%Y-%m-%d"))
        if fin:
            finance_db.add_operation(
                message.chat.id, fin["amount"], fin["category"], fin["comment"], fin["date"]
            )
            await deliver(
                message, f"💸 <b>Записал:</b> {_money(fin['amount'])} · {ui.esc(fin['category'])}"
            )
            return

    # всё остальное — чат с ассистентом (история + авто-факты внутри)
    await _assistant_reply(message, text)
