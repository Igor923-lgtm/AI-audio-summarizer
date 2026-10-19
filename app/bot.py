import os
import re
import logging
import asyncio
from datetime import datetime, timedelta, timezone
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
ALLOWED_IDS = settings.allowed_ids


@dp.update.outer_middleware()
async def whitelist_middleware(handler, event, data):
    if ALLOWED_IDS:
        user = None
        if hasattr(event, "message") and event.message:
            user = event.message.from_user
        elif hasattr(event, "callback_query") and event.callback_query:
            user = event.callback_query.from_user
        if user is None or user.id not in ALLOWED_IDS:
            return None
    return await handler(event, data)


# ---------- утилиты ----------

def _sanitize_filename(name: str) -> str:
    name = os.path.splitext(name)[0]
    clean = re.sub(r"[^a-zA-Z0-9_\-а-яА-ЯіІїЇєЄ]", "_", name).strip("_")
    return clean or "note"


def _save_note(markdown_report: str, display_name: str) -> str:
    os.makedirs(NOTES_DIR, exist_ok=True)
    from datetime import datetime as _dt

    stamp = _dt.now().strftime("%Y-%m-%d_%H%M%S")
    file_name = f"{stamp}_{_sanitize_filename(display_name)}.md"
    file_path = os.path.join(NOTES_DIR, file_name)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(markdown_report)
    logger.info(f"Заметка сохранена: {file_path}")
    return file_path


def _list_notes() -> list[str]:
    if not os.path.isdir(NOTES_DIR):
        return []
    return sorted(
        (os.path.join(NOTES_DIR, f) for f in os.listdir(NOTES_DIR) if f.endswith(".md")),
        key=os.path.getmtime,
        reverse=True,
    )


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


async def deliver(message: types.Message, text: str, reply_markup=None):
    """Отправка ответа: всегда текст, при включённом голосе — ещё и голосовое."""
    await message.answer(text, reply_markup=reply_markup)
    if kv.voice_enabled(message.chat.id):
        # Для озвучки убираем HTML-теги
        plain = re.sub(r"<[^>]+>", "", text)
        plain = re.sub(r"[^\S\n]+", " ", plain).strip()
        audio = await tts.synthesize(plain, kv.voice_key(message.chat.id))
        if audio:
            try:
                await message.answer_voice(
                    types.BufferedInputFile(audio, filename="reply.mp3")
                )
            except Exception as e:
                logger.error(f"Не удалось отправить голос: {e}")


# ---------- главное меню / старт / помощь ----------

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    await message.answer(
        ui.card(
            "Личный ассистент",
            "Голосовое → чистая заметка. Напоминания, задачи и финансы — голосом или текстом.\n\n"
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
    files = _list_notes()
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
    files = _list_notes()
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
        f"{html_text}\n\n<i>{ui.esc(os.path.basename(path))}</i>",
        reply_markup=ui.note_pager_kb(idx, len(files)),
    )


@dp.callback_query(F.data.startswith("ui:notes:file:"))
async def cb_notes_file(call: types.CallbackQuery):
    idx = int(call.data.rsplit(":", 1)[1])
    await call.answer("Отправляю файл")
    files = _list_notes()
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
        await message.answer(empty_text)
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
        await call.message.answer("Активных задач нет. Создай: «задача: ...»")
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
        await call.message.answer("Сделанных задач пока нет.")
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
        f"💰 <b>Баланс · {month}</b>\n\n"
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
        await call.message.answer(f"За {month} операций пока нет.")
        return
    lines = [f"📊 <b>Категории · {month}</b>\n"]
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
        await call.message.answer("Операций пока нет.")
        return
    lines = ["🧾 <b>Последние операции</b>\n"]
    for r in rows:
        comment = f" — {ui.esc(r['comment'])}" if r["comment"] else ""
        lines.append(f"#{r['id']} {r['occurred_on']} · {_money(r['amount'])} · {ui.esc(r['category'])}{comment}")
    await call.message.answer("\n".join(lines))


# ---------- обработка голосовых ----------

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

        note_path = _save_note(markdown_report, display_name)

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
    files = _list_notes()
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
    for path in _list_notes():
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


@dp.message(Command("reminders"))
async def cmd_reminders(message: types.Message):
    rows = reminders_db.list_pending(message.chat.id)
    await _send_reminders(message, rows, "Активных напоминаний нет.")


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
        f"💰 <b>Баланс · {month}</b>\n\n"
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
        await message.answer(f"За {month} операций пока нет.")
        return
    lines = [f"📊 <b>Категории · {month}</b>\n"]
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
        await message.answer("Операций пока нет.")
        return
    lines = [f"🧾 <b>Операции</b>\n"]
    for r in rows:
        lines.append(f"#{r['id']} {r['occurred_on']} · {_money(r['amount'])} · {ui.esc(r['category'])}")
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

    # заметки: релевантные по запросу, иначе — 5 последних
    note_lines: list[str] = []
    if query:
        for hit in memory.search_notes(NOTES_DIR, query, limit=3):
            note_lines.append(f"- {os.path.basename(hit['path'])}: {hit['snippet'][:300]}")
    if not note_lines:
        for path in _list_notes()[:5]:
            try:
                with open(path, encoding="utf-8") as f:
                    note_lines.append(
                        f"- {os.path.basename(path)}: {f.read()[:300].replace(chr(10), ' ').strip()}"
                    )
            except Exception:
                continue
    if note_lines:
        parts.append("Заметки:\n" + "\n".join(note_lines))

    hist = memory.history_context(chat_id, limit=12)
    if hist:
        parts.append(hist)

    return "\n".join(parts)


# ---------- текст: напоминание / задача / финансы / чат ----------

def _make_tool_executor(chat_id: int):
    """Возвращает async-диспетчер инструментов агента, привязанный к чату."""

    async def execute_tool(name: str, args: dict) -> str:
        if name == "web_search":
            return await tools_search.web_search(str(args.get("query", "")))
        if name == "search_notes":
            hits = memory.search_notes(NOTES_DIR, str(args.get("query", "")), limit=4)
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

    # всё остальное — чат с ассистентом (агент: сам зовёт инструменты)
    memory.add_dialog(message.chat.id, "user", text)
    try:
        context = await _build_context(message.chat.id, text)
        reply = await ai_service.chat(
            text,
            context,
            execute_tool=_make_tool_executor(message.chat.id),
            skills=skills.load_skills(),
        )
        memory.add_dialog(message.chat.id, "assistant", reply)
        await deliver(message, ui.esc(reply))
    except Exception as e:
        logger.error(f"Ошибка чата: {e}")
        await message.answer("❌ Не получилось ответить. Попробуй ещё раз.")
