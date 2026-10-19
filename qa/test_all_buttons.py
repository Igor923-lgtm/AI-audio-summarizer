"""Интеграционный тест всех кнопок/handler'ов через aiogram feed_update с мок-ботом."""
import asyncio
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

# изолируем БД и заметки ДО импорта app
TEST_DIR = "/tmp/bottest"
shutil.rmtree(TEST_DIR, ignore_errors=True)
os.makedirs(TEST_DIR + "/notes", exist_ok=True)
os.environ["REMINDERS_DB"] = f"{TEST_DIR}/reminders.db"
os.environ["FINANCE_DB"] = f"{TEST_DIR}/finance.db"
os.environ["TASKS_DB"] = f"{TEST_DIR}/tasks.db"
os.environ["MEMORY_DB"] = f"{TEST_DIR}/memory.db"
os.environ["NOTES_DIR"] = f"{TEST_DIR}/notes"
os.environ["TELEGRAM_BOT_TOKEN"] = "123:test"
os.environ["GROQ_API_KEY"] = "gsk_test"
os.environ["ALLOWED_USER_IDS"] = "1"

# тестовые заметки
for i, title in enumerate(["План запуска", "Идеи для продукта", "Встреча с Светой"], 1):
    with open(f"{TEST_DIR}/notes/2026-10-1{i}_120000_note{i}.md", "w") as f:
        f.write(f"# {title}\n\n## Самое важное\n- пункт {i}\n\nТекст заметки {i}.\n")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot, Dispatcher
from aiogram.types import Update

import app.bot as B
from app.bot import dp
from app import reminders as R
from app import tasks as T
from app import finance as F
from app import memory as MEM

# ---- мок-бот ----
# В aiogram 3.x шорткаты (message.answer) возвращают объект метода, который
# исполняется через вызов бота: await method.as_(bot) → bot(method).
# Поэтому записи — в mock_bot.call_args_list.
mock_bot = AsyncMock(spec=Bot)
mock_bot.id = 1


def sent_methods():
    return [c.args[0] for c in mock_bot.call_args_list if c.args]


def sent_texts():
    out = []
    for m in sent_methods():
        t = getattr(m, "text", None) or getattr(m, "caption", None)
        if t:
            out.append(str(t))
    return out


def last_text():
    return sent_texts()[-1] if sent_texts() else ""


def last_method():
    ms = sent_methods()
    return ms[-1] if ms else None


def sent_docs():
    return [m for m in sent_methods() if getattr(m, "document", None) is not None]


# ---- заглушки LLM (без сети) ----
TZ = timezone(timedelta(hours=3))
B.ai_service.parse_reminder = AsyncMock(
    return_value={"when": datetime(2026, 10, 20, 15, 0, tzinfo=TZ), "text": "позвонить врачу", "repeat": None}
)
B.ai_service.parse_task = AsyncMock(return_value="сдать отчёт")
B.ai_service.parse_finance = AsyncMock(
    return_value={"amount": -500, "category": "такси", "comment": "до офиса", "date": "2026-10-19"}
)
B.ai_service.structure_note = AsyncMock(return_value="# Тестовая заметка\n\n## Самое важное\n- всё хорошо\n\nТело.\n")
B.ai_service.chat = AsyncMock(return_value="Привет! Я на связи.")
B.ai_service.extract_fact = AsyncMock(return_value="Игорь живёт в Москве")

USER = {"id": 1, "is_bot": False, "first_name": "Тест", "username": "tester"}
CHAT = {"id": 1, "type": "private", "first_name": "Тест"}


def msg_update(text: str, mid: int = 1) -> Update:
    return Update.model_validate(
        {"update_id": mid, "message": {"message_id": mid, "date": "2026-10-19T12:00:00", "from": USER, "chat": CHAT, "text": text}},
        context={"bot": mock_bot},
    )


def cb_update(data: str, uid: int = 100, mid: int = 2) -> Update:
    return Update.model_validate(
        {
            "update_id": uid,
            "callback_query": {
                "id": str(uid),
                "from": USER,
                "chat_instance": "ci",
                "data": data,
                "message": {
                    "message_id": mid,
                    "date": "2026-10-19T12:00:00",
                    "from": {"id": 8945442457, "is_bot": True, "first_name": "Бот"},
                    "chat": CHAT,
                    "text": "старое сообщение",
                },
            },
        },
        context={"bot": mock_bot},
    )


results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))


async def feed(u: Update):
    await dp.feed_update(mock_bot, u)


async def main():
    # 1. /start
    await feed(msg_update("/start"))
    check("start: приветствие", "ассистент" in last_text().lower())
    check("start: меню", getattr(last_method(), "reply_markup", None) is not None)

    # 2. Главные кнопки меню
    for label, marker in [
        ("📝 Заметки", "Заметки"),
        ("⏰ Напоминания", "Напоминания"),
        ("✅ Задачи", "Задачи"),
        ("💸 Финансы", "Финансы"),
        ("⚙️ Помощь", "Что умею"),
    ]:
        await feed(msg_update(label))
        check(f"кнопка «{label}»", marker.lower() in last_text().lower(), last_text()[:80])

    # 3. Заметки
    await feed(cb_update("ui:notes:last", 10))
    check("notes:last: заголовок", "План запуска" in last_text() or "🔖" in last_text(), last_text()[:120])
    check("notes:last: нет markdown-сырья", "##" not in last_text())
    check("notes:last: есть pager", getattr(last_method(), "reply_markup", None) is not None)

    first_note_text = last_text()
    await feed(cb_update("ui:notes:next:1", 11))
    check("notes:next: страница сменилась", last_text() != first_note_text)
    kb = getattr(last_method(), "reply_markup", None)
    labels = [b.text for row in (kb.inline_keyboard if kb else []) for b in row]
    check("notes:next: метка 2/3", "2/3" in labels, str(labels))
    second_note_text = last_text()
    await feed(cb_update("ui:notes:prev:0", 12))
    check("notes:prev: вернулись", last_text() == first_note_text)
    check("notes:prev: метка 1/3", "1/3" in [b.text for row in (getattr(last_method(), "reply_markup", None).inline_keyboard) for b in row])
    n_docs_before = len(sent_docs())
    await feed(cb_update("ui:notes:file:0", 13))
    check("notes:file", len(sent_docs()) == n_docs_before + 1)
    await feed(cb_update("ui:notes:search", 14))
    check("notes:search", "слов" in last_text().lower() or "напиши" in last_text().lower())

    # чат-режим: обычное сообщение → ответ ассистента
    await feed(msg_update("привет, расскажи шутку"))
    check("чат: ответ ассистента", "на связи" in last_text(), last_text()[:80])
    check("чат: вызов LLM", B.ai_service.chat.called)

    # память: «запомни ...» → факт в БД + подтверждение
    await feed(msg_update("запомни, что я живу в Москве", 90))
    facts = MEM.list_facts(1)
    check("память: факт сохранён", any("Москв" in f for f in facts), str(facts))
    check("память: подтверждение", "записал" in last_text().lower(), last_text()[:80])

    # память: «что ты помнишь?» → список фактов
    await feed(msg_update("что ты помнишь?", 91))
    check("память: ответ списком", "Москв" in last_text(), last_text()[:120])

    # память: FTS-поиск по заметкам находит по слову (слово — в snippet, не в имени файла)
    hits = MEM.search_notes(f"{TEST_DIR}/notes", "план", 3)
    check(
        "память: FTS находит заметку",
        len(hits) > 0 and any("План" in h["snippet"] for h in hits),
        str(hits)[:120],
    )

    # память: история диалога пишет chat-реплики (user + assistant)
    hist = MEM.history_context(1, 20)
    check("память: история диалога", "привет" in hist.lower() and "на связи" in hist.lower(), hist[:120])

    # --- T3: диспетчер инструментов агента (офлайн) ---
    exec_fn = B._make_tool_executor(1)
    notes_res = await exec_fn("search_notes", {"query": "план"})
    check("инстр: search_notes", "План" in notes_res or "note1" in notes_res, notes_res[:100])
    bal_res = await exec_fn("get_balance", {})
    check("инстр: get_balance", "баланс" in bal_res.lower() or "₽" in bal_res, bal_res[:100])
    unk_res = await exec_fn("no_such_tool", {})
    check("инстр: неизвестный отклонён", "неизвестный" in unk_res.lower(), unk_res[:80])

    # --- T3: агент-цикл (мок LLM: tool_call → финальный ответ) ---
    from types import SimpleNamespace
    from app.ai_service import AIService

    ai = AIService()
    n_calls = {"n": 0}

    async def fake_create(**kw):
        n_calls["n"] += 1
        if n_calls["n"] == 1:
            tc = SimpleNamespace(id="t1", function=SimpleNamespace(name="get_balance", arguments="{}"))
            msg = SimpleNamespace(content=None, tool_calls=[tc])
        else:
            msg = SimpleNamespace(content="Баланс: остаток +48300 ₽.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    ai.client.chat.completions.create = fake_create
    ran = []

    async def fake_exec(name, args):
        ran.append(name)
        return "доходы +50000 ₽"

    reply = await ai.chat("сколько у меня денег?", context="", execute_tool=fake_exec)
    check("агент: инструмент вызван", ran == ["get_balance"], str(ran))
    check("агент: финальный ответ", "баланс" in reply.lower(), reply[:80])
    check("агент: ровно 2 итерации цикла", n_calls["n"] == 2, str(n_calls["n"]))

    # 4. Напоминания
    await feed(cb_update("ui:rem:today", 20))
    check("rem:today пусто", "нет" in last_text().lower(), last_text()[:80])
    await feed(cb_update("ui:rem:all", 21))
    check("rem:all пусто", "нет" in last_text().lower())
    await feed(cb_update("ui:rem:help", 22))
    check("rem:help", "каждый день" in last_text())

    # создание напоминания текстом
    await feed(msg_update("напомни завтра в 15:00 позвонить врачу", 30))
    check("создание напоминания", "напоминание #1" in last_text().lower(), last_text()[:120])
    rows = R.list_pending(1)
    check("БД: напоминание создано", len(rows) == 1 and rows[0]["text"] == "позвонить врачу")

    # P1-fix: каждое напоминание — отдельное сообщение со СВОЕЙ кнопкой отмены
    extra_id = R.add_reminder(1, "второе напоминание", datetime.now(timezone.utc) + timedelta(days=1))
    n_before = len(sent_methods())
    await feed(cb_update("ui:rem:all", 31))
    new_methods = [m for m in sent_methods()[n_before:] if getattr(m, "reply_markup", None)]
    check("rem:all: по 1 сообщению на напоминание", len(new_methods) == 2, f"{len(new_methods)}")
    cancel_ids = {
        b.callback_data
        for m in new_methods
        for row in m.reply_markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("ui:rem:cancel:")
    }
    check(
        "rem:all: у каждого своя кнопка отмены",
        cancel_ids == {f"ui:rem:cancel:1", f"ui:rem:cancel:{extra_id}"},
        str(cancel_ids),
    )
    new_texts = [getattr(m, "text", "") for m in new_methods]
    check(
        "rem:all: список содержит оба",
        "позвонить" in " ".join(new_texts) and "второе" in " ".join(new_texts),
        str(new_texts),
    )
    # убираем лишнее напоминание, чтобы вернуть состояние (только #1 pending)
    R.cancel(extra_id, 1)
    await feed(cb_update("ui:rem:today", 32))

    # P1-fix: toggle голоса обновляет ТЕКСТ help (а не только кнопку)
    n_before = len(sent_texts())
    await feed(cb_update("ui:voice:toggle", 39))
    check("голос toggle: toast", any("Голос включён" in t for t in sent_texts()[n_before:]))
    check("голос toggle: текст help обновился", "включены" in last_text().lower(), last_text()[:120])
    await feed(cb_update("ui:voice:toggle", 38))  # вернуть выкл
    check("голос toggle: текст help вернулся", "выключены" in last_text().lower(), last_text()[:120])

    # snooze через callback
    await feed(cb_update("ui:rem:snooze:1:3600", 33))
    check("snooze +1ч", "перенес" in last_text().lower(), last_text()[:80])
    rows = R.list_pending(1)
    check("snooze: due сдвинулся", rows and rows[0]["due_at"] > datetime.now(timezone.utc).isoformat())

    # отмена
    await feed(cb_update("ui:rem:cancel:1", 34))
    check("rem:cancel", "отменено" in last_text().lower() or "отменено" in str(mock_bot.call_args_list))
    check("rem:cancel: в БД", len(R.list_pending(1)) == 0)

    # done (создать новое, пометить готово)
    rid = R.add_reminder(1, "второе", datetime.now(timezone.utc) - timedelta(minutes=1))
    await feed(cb_update(f"ui:rem:done:{rid}", 35))
    check("rem:done", "готово" in last_text().lower(), last_text()[:80])

    # snooze после done (проверка починенного бага)
    await feed(cb_update(f"ui:rem:snooze:{rid}:86400", 36))
    check("snooze после done", len(R.list_pending(1)) == 1, str([dict(r) for r in R.list_pending(1)]))

    # 5. Задачи
    await feed(msg_update("задача: сдать отчёт до пятницы", 40))
    check("создание задачи", "задача #1" in last_text().lower(), last_text()[:100])
    check("БД: задача", T.count_active(1) == 1)
    await feed(cb_update("ui:tasks:active", 41))
    check("tasks:active", "сдать отчёт" in last_text())
    await feed(cb_update("ui:tasks:done:1", 42))
    check("tasks:done", "выполнена" in last_text().lower(), last_text()[:80])
    check("tasks: done в БД", T.count_active(1) == 0)
    await feed(cb_update("ui:tasks:done", 43))
    check("tasks:done-список", "сдать отчёт" in last_text())
    await feed(cb_update("ui:tasks:undone:1", 44))
    check("tasks:undone", "снова" in last_text().lower())
    await feed(cb_update("ui:tasks:active", 45))
    check("tasks:active снова", "сдать отчёт" in last_text())

    # 6. Финансы
    await feed(msg_update("потратил 500 на такси", 50))
    check("финансы: запись", "записал" in last_text().lower() and "500" in last_text(), last_text()[:100])
    check("БД: операция", F.month_summary(1, "2026-10")["expense"] == 500)
    await feed(cb_update("ui:fin:balance", 51))
    check("fin:balance", "баланс" in last_text().lower() and "500" in last_text(), last_text()[:120])
    await feed(cb_update("ui:fin:report", 52))
    check("fin:report", "такси" in last_text())
    await feed(cb_update("ui:fin:list", 53))
    check("fin:list", "такси" in last_text() or "#1" in last_text())

    # команды-алиасы
    await feed(msg_update("/баланс", 54))
    check("/баланс", "баланс" in last_text().lower())
    await feed(msg_update("/отчёт", 55))
    check("/отчёт", "категории" in last_text().lower() or "такси" in last_text())
    await feed(msg_update("/расходы", 56))
    check("/расходы", "такси" in last_text())
    n_before = len(sent_methods())
    await feed(msg_update("/reminders", 57))
    texts = [getattr(m, "text", "") for m in sent_methods()[n_before:]]
    check(
        "/reminders",
        any("напоминания" in t.lower() or "нет" in t.lower() for t in texts),
        str(texts)[:120],
    )
    await feed(msg_update("/cancel 999", 58))
    check("/cancel 999", "не найдено" in last_text().lower())
    await feed(msg_update("/last 2", 59))
    check("/last 2", len(sent_docs()) >= 2)
    await feed(msg_update("/find план", 60))
    check("/find", "найдено" in last_text().lower())
    await feed(msg_update("/help", 61))
    check("/help", "умею" in last_text().lower())

    # 7. menu/noop
    await feed(cb_update("ui:menu", 70))
    check("ui:menu", "ассистент" in last_text().lower() or "разделы" in last_text().lower())
    await feed(cb_update("ui:noop", 71))
    check("ui:noop: не упал", True)

    # 8. whitelist: чужак молчит
    upd = Update.model_validate(
        {"update_id": 200, "message": {"message_id": 200, "date": "2026-10-19T12:00:00",
         "from": {"id": 999, "is_bot": False, "first_name": "Чужой"}, "chat": {"id": 999, "type": "private"}, "text": "/start"}},
        context={"bot": mock_bot},
    )
    n_before = len(sent_texts())
    await feed(upd)
    check("whitelist: чужой игнорируется", len(sent_texts()) == n_before)

    # 9. цикл уведомлений (моck: due напоминание → send_message с кнопками)
    from app import main as M
    mock_bot.reset_mock()
    R.add_reminder(1, "срочно позвонить", datetime.now(timezone.utc) - timedelta(seconds=5))
    await M.send_due_reminders(mock_bot)
    # уведомления шлются прямым bot.send_message (не шорткатом)
    dm_calls = mock_bot.send_message.call_args_list
    dm_texts = [str(c.args[1]) if len(c.args) > 1 else "" for c in dm_calls]
    check("цикл: уведомление отправлено", any("срочно позвонить" in t for t in dm_texts), str(dm_texts)[:200])
    rm = dm_calls[0].kwargs.get("reply_markup") if dm_calls else None
    check("цикл: кнопки snooze", rm is not None)

    # итог
    fails = [r for r in results if not r[1]]
    print(f"\n=== {len(results) - len(fails)}/{len(results)} OK, {len(fails)} FAIL ===")
    for name, _, detail in fails:
        print(f"  FAIL: {name} {detail}")
    sys.exit(1 if fails else 0)


asyncio.run(main())
