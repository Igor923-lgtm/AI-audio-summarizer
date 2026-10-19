#!/usr/bin/env python3
"""Визуальный QA @igorkyz_bot — одна команда, идемпотентно.

    ./venv/bin/python qa/run_qa.py            # весь конвейер
    ./venv/bin/python qa/run_qa.py --no-shots # только screens.json + pages (без браузера)

Что делает (всё в одном процессе, без ручных http/playwright):
  1. стирает ТОЛЬКО qa/state/ (код и страницы-артефакты не трогает);
  2. сид-данные + мок-LLM → реальные выводы хендлеров → qa/state/screens.json;
  3. рендерит Telegram-iOS макет (390px) → qa/state/pages/<id>.html (функцией, не отдельным скриптом);
  4. поднимает http-сервер на свободном порту (ThreadedHTTPServer, wait-ready, shutdown в finally);
  5. playwright-cli: скриншот каждого экрана --full-page → qa/state/shots/<id>.png;
  6. валидирует КАЖДЫЙ PNG (байты ≥ MIN_PNG и ширина ≥ MIN_W) — пустой = fail loud;
  7. пишет qa/REPORT.md.

Контрольный счёт: EXPECTED экранов, 0 blank, 0 ошибок отрисовки.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import html
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

# ---- пути (это сам qa/run_qa.py → PROJECT = родитель qa/) ----
QA_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(QA_DIR)
STATE = os.path.join(QA_DIR, "state")
DB_DIR = os.path.join(STATE, "db")
NOTES_DIR = os.path.join(DB_DIR, "notes")
PAGES = os.path.join(STATE, "pages")
SHOTS = os.path.join(STATE, "shots")
REPORT = os.path.join(QA_DIR, "REPORT.md")

PWCLI = os.environ.get(
    "PWCLI",
    "/Users/igor/.local/share/mimocode/builtin_skills/0.1.15/skills/playwright/scripts/playwright_cli.sh",
)
VIEWPORT_W, VIEWPORT_H = 390, 844
MIN_PNG_BYTES = 6_000
MIN_PNG_W = 300
EXPECTED_SCREENS = 32

sys.path.insert(0, PROJECT)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 1. state
def setup_state() -> None:
    """Стираем и готовим ТОЛЬКО qa/state. Код QA и app/ не трогаем."""
    for d in (DB_DIR, NOTES_DIR, PAGES, SHOTS):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    os.chdir(DB_DIR)  # main.py пишет digest_state.txt в cwd
    for key, val in {
        "REMINDERS_DB": f"{DB_DIR}/reminders.db",
        "FINANCE_DB": f"{DB_DIR}/finance.db",
        "TASKS_DB": f"{DB_DIR}/tasks.db",
        "MEMORY_DB": f"{DB_DIR}/memory.db",
        "KV_DB": f"{DB_DIR}/kv.db",
        "NOTES_DIR": NOTES_DIR,
        "TELEGRAM_BOT_TOKEN": "123:test",
        "GROQ_API_KEY": "gsk_test",
        "ALLOWED_USER_IDS": "1,2",
    }.items():
        os.environ[key] = val


# ---------------------------------------------------------------- 2. capture
NOTES = [
    (
        "2026-10-19_120000_План_запуска.md",
        "# План запуска\n\n## Самое важное\n- [ ] выйти на 10 клиентов\n- [x] "
        "сделать лендинг\n- [ ] запустить рекламу\n\n**Бюджет** 500к, сроки "
        "*жёсткие*, всё решаем на созвоне в `понедельник`.\n\n## Задачи\n"
        "- [ ] смета до пятницы\n",
    ),
    (
        "2026-10-18_093000_Идеи.md",
        "# Идеи для продукта\n\n## Самое важное\n- бот-напоминания с повторами\n"
        "- *автосводка* утром в 9:00\n- отчёт по финансам за неделю\n\n"
        "Обычный абзац с `кодом` внутри и эмодзи 🚀, длинное-очень-длинное-слово-"
        "для-проверки-переноса-строки-в-карточке.\n",
    ),
    (
        "2026-10-17_181500_Встреча_со_Светой.md",
        "# Встреча с Светой\n\nОбсудили договор, *цена* вопроса, **дедлайн** — "
        "пятница.\n\n- задача: прислать смету\n- задача: уточнить сроки\n",
    ),
]


def capture() -> list[dict]:
    """Кормим апдейты в реальные хендлеры (мок-бот, мок-LLM) → список экранов."""
    from aiogram import Bot
    from aiogram.types import Update

    import app.bot as B
    from app.bot import dp
    import app.main as M
    from app import reminders as R
    from app import tasks as T
    from app import finance as F

    for name, body in NOTES:
        with open(os.path.join(NOTES_DIR, name), "w", encoding="utf-8") as f:
            f.write(body)

    mock_bot = AsyncMock(spec=Bot)
    mock_bot.id = 1

    def sent_methods():
        return [c.args[0] for c in mock_bot.call_args_list if c.args]

    def direct_sends():
        return [
            (c.args[1] if len(c.args) > 1 else "", c.kwargs.get("reply_markup"))
            for c in mock_bot.send_message.call_args_list
        ]

    def kb_info(markup):
        if markup is None:
            return ("none", [])
        if hasattr(markup, "inline_keyboard"):
            return ("inline", [[b.text for b in row] for row in markup.inline_keyboard])
        if hasattr(markup, "keyboard"):
            return ("reply", [[b.text for b in row] for row in markup.keyboard])
        if getattr(markup, "remove", False):
            return ("none", [])
        return ("other", [])

    def method_to_msg(m):
        text = getattr(m, "text", None) or getattr(m, "caption", None)
        kbt, rows = kb_info(getattr(m, "reply_markup", None))
        has_doc = getattr(m, "document", None) is not None
        return {"text": text or ("[документ]" if has_doc else ""), "kb_type": kbt, "kb": rows}

    TZ = timezone(timedelta(hours=3))
    now_msk = datetime.now(TZ)
    B.ai_service.parse_reminder = AsyncMock(
        return_value={
            "when": (now_msk + timedelta(days=1)).replace(hour=15, minute=0),
            "text": "позвонить врачу",
            "repeat": None,
        }
    )
    B.ai_service.parse_task = AsyncMock(return_value="сдать отчёт до пятницы")
    B.ai_service.parse_finance = AsyncMock(
        return_value={
            "amount": -500,
            "category": "такси",
            "comment": "до офиса",
            "date": now_msk.strftime("%Y-%m-%d"),
        }
    )
    B.ai_service.chat = AsyncMock(
        return_value="Привет! Я на связи. Расскажу шутку: программист заходит в бар, "
        "видит строку — говорит «возьми мне стакан воды». 🍺"
    )
    B.ai_service.structure_note = AsyncMock(return_value="# Заметка\n\nТело.\n")

    USER1 = {"id": 1, "is_bot": False, "first_name": "Игорь", "username": "igor"}
    USER2 = {"id": 2, "is_bot": False, "first_name": "Гость", "username": "guest"}
    CHAT1 = {"id": 1, "type": "private", "first_name": "Игорь"}
    CHAT2 = {"id": 2, "type": "private", "first_name": "Гость"}

    screens: list[dict] = []

    def capture_screen(sid, title, msgs, user_msg=None, trigger=None):
        kept = [m for m in msgs if (m["text"] or "").strip() or m.get("kb")]
        screens.append(
            {"id": sid, "title": title, "user_msg": user_msg, "trigger": trigger, "msgs": kept}
        )

    def msg_update(text, mid=1, user=None, chat=None):
        return Update.model_validate(
            {
                "update_id": mid,
                "message": {
                    "message_id": mid,
                    "date": "2026-10-19T12:00:00",
                    "from": user or USER1,
                    "chat": chat or CHAT1,
                    "text": text,
                },
            },
            context={"bot": mock_bot},
        )

    def cb_update(data, uid=100, mid=2, user=None, chat=None):
        return Update.model_validate(
            {
                "update_id": uid,
                "callback_query": {
                    "id": str(uid),
                    "from": user or USER1,
                    "chat_instance": "ci",
                    "data": data,
                    "message": {
                        "message_id": mid,
                        "date": "2026-10-19T12:00:00",
                        "from": {"id": 8945442457, "is_bot": True, "first_name": "Бот"},
                        "chat": chat or CHAT1,
                        "text": "старое сообщение",
                    },
                },
            },
            context={"bot": mock_bot},
        )

    async def feed_capture(sid, title, update, user_msg=None, trigger=None):
        n0 = len(sent_methods())
        await dp.feed_update(mock_bot, update)
        new = [method_to_msg(m) for m in sent_methods()[n0:]]
        capture_screen(sid, title, new, user_msg=user_msg, trigger=trigger)

    async def main():
        R.add_reminder(
            1, "позвонить врачу по страховке",
            (now_msk + timedelta(days=1)).replace(hour=15, minute=0).astimezone(timezone.utc),
        )
        R.add_reminder(
            1, "тренировка",
            (now_msk + timedelta(days=3)).replace(hour=18, minute=0).astimezone(timezone.utc),
            repeat_rule="weekly:3",
        )
        T.add_task(1, "доделать смету для Светы")
        T.set_done(T.add_task(1, "купить билеты"), 1, True)
        today = now_msk.strftime("%Y-%m-%d")
        F.add_operation(1, -500, "такси", "до офиса", today)
        F.add_operation(1, -1200, "офис", "кофе и печеньки", today)
        F.add_operation(1, 50000, "фриланс", "аванс за проект", today)

        await feed_capture("S01", "/start", msg_update("/start", mid=1), user_msg="/start")
        await feed_capture("S02", "/help (голос выкл)", msg_update("/help", mid=2), user_msg="/help")
        await feed_capture("S02a", "голос toggle (toast + текст help)", cb_update("ui:voice:toggle", uid=10, mid=3))
        await feed_capture("S02b", "/help (голос вкл)", msg_update("/help", mid=4), user_msg="/help")
        await feed_capture("S02c", "голос toggle выкл", cb_update("ui:voice:toggle", uid=11, mid=5))
        for sid, label in [("S05", "📝 Заметки"), ("S06", "⏰ Напоминания"), ("S07", "✅ Задачи"), ("S08", "💸 Финансы")]:
            await feed_capture(sid, f"кнопка «{label}»", msg_update(label, mid=20), user_msg=label)
        await feed_capture("S09", "ui:меню (домой)", cb_update("ui:menu", uid=30), trigger="ui:menu")
        await feed_capture("S11", "карточка заметки (1/3)", cb_update("ui:notes:next:0", uid=40), trigger="ui:notes:next:0")
        await feed_capture("S11b", "карточка заметки (2/3)", cb_update("ui:notes:next:1", uid=41), trigger="ui:notes:next:1")
        await feed_capture("S15", "подсказка поиска", cb_update("ui:notes:search", uid=42), trigger="ui:notes:search")
        await feed_capture("S16", "список напоминаний (2 шт)", cb_update("ui:rem:all", uid=50), trigger="ui:rem:all")
        await feed_capture("S17", "сегодня: пусто", cb_update("ui:rem:today", uid=51), trigger="ui:rem:today")
        await feed_capture("S19", "как создать напоминание", cb_update("ui:rem:help", uid=52), trigger="ui:rem:help")
        await feed_capture("S18", "все: пусто (чужой)", cb_update("ui:rem:all", uid=53, user=USER2, chat=CHAT2), trigger="ui:rem:all")
        await feed_capture("S23", "активные задачи", cb_update("ui:tasks:active", uid=60), trigger="ui:tasks:active")
        await feed_capture("S25", "сделанные задачи", cb_update("ui:tasks:done", uid=61), trigger="ui:tasks:done")
        await feed_capture("S24", "активных нет (чужой)", cb_update("ui:tasks:active", uid=62, user=USER2, chat=CHAT2), trigger="ui:tasks:active")
        await feed_capture("S26", "сделанных нет (чужой)", cb_update("ui:tasks:done", uid=63, user=USER2, chat=CHAT2), trigger="ui:tasks:done")
        await feed_capture("S29", "баланс", cb_update("ui:fin:balance", uid=70), trigger="ui:fin:balance")
        await feed_capture("S30", "отчёт по категориям", cb_update("ui:fin:report", uid=71), trigger="ui:fin:report")
        await feed_capture("S32", "последние операции", cb_update("ui:fin:list", uid=72), trigger="ui:fin:list")
        await feed_capture("S31", "категории: пусто (чужой)", cb_update("ui:fin:report", uid=73, user=USER2, chat=CHAT2), trigger="ui:fin:report")
        await feed_capture("S33", "операции: пусто (чужой)", cb_update("ui:fin:list", uid=74, user=USER2, chat=CHAT2), trigger="ui:fin:list")
        await feed_capture("S43", "напоминание текстом", msg_update("напомни завтра в 15:00 позвонить врачу", mid=80), user_msg="напомни завтра в 15:00 позвонить врачу")
        await feed_capture("S44", "задача текстом", msg_update("задача: сдать отчёт до пятницы", mid=81), user_msg="задача: сдать отчёт до пятницы")
        await feed_capture("S45", "финансы текстом", msg_update("потратил 500 на такси", mid=82), user_msg="потратил 500 на такси")
        await feed_capture("S46", "чат-ответ ассистента", msg_update("привет, расскажи шутку", mid=83), user_msg="привет, расскажи шутку")

        R.add_reminder(1, "срочно позвонить в банк", datetime.now(timezone.utc) - timedelta(seconds=5))
        mock_bot.reset_mock()
        await M.send_due_reminders(mock_bot)
        capture_screen("PUSH01", "push-напоминание (snooze)",
                       [{"text": t, **dict(zip(("kb_type", "kb"), kb_info(k)))} for t, k in direct_sends()],
                       trigger="cron: due")

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                d = cls(2026, 10, 19, 10, 0, 0)
                return d.replace(tzinfo=tz) if tz else d

        today_21 = now_msk.replace(hour=21, minute=0, second=0, microsecond=0)
        R.add_reminder(1, "встреча в 12:00", min(now_msk + timedelta(hours=1), today_21))
        mock_bot.reset_mock()
        real_dt = M.datetime
        M.datetime = FakeDT
        try:
            await M.send_digest_if_needed(mock_bot, 1)
        finally:
            M.datetime = real_dt
        capture_screen("PUSH02", "утренний digest 09:00",
                       [{"text": t, **dict(zip(("kb_type", "kb"), kb_info(k)))} for t, k in direct_sends()],
                       trigger="cron: digest")

    asyncio.run(main())
    with open(os.path.join(STATE, "screens.json"), "w", encoding="utf-8") as f:
        json.dump(screens, f, ensure_ascii=False, indent=1)
    empty = [s["id"] for s in screens if not s["msgs"]]
    log(f"[capture] {len(screens)} экранов, {sum(len(s['msgs']) for s in screens)} сообщений")
    if empty:
        raise SystemExit(f"[capture] FAIL: пустые экраны: {empty}")
    if len(screens) != EXPECTED_SCREENS:
        raise SystemExit(f"[capture] FAIL: {len(screens)} экранов, ожидалось {EXPECTED_SCREENS}")
    return screens


# ---------------------------------------------------------------- 3. render
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #d1d7dc; width: 390px; margin: 0 auto; color: #000; }
.tg-header { background: #f6f6f6; border-bottom: 1px solid #d9d9d9; padding: 10px 14px; text-align: center; }
.tg-header .name { font-weight: 600; font-size: 15px; }
.tg-header .status { font-size: 12px; color: #7a7a7a; }
.chat { padding: 12px 10px 8px; display: flex; flex-direction: column; gap: 6px; min-height: 420px;
  background: #e3e7eb;
  background-image: radial-gradient(circle at 20% 30%, rgba(255,255,255,.5) 0 2px, transparent 2px),
                    radial-gradient(circle at 70% 60%, rgba(255,255,255,.4) 0 2px, transparent 2px);
  background-size: 90px 90px, 130px 130px; }
.day { align-self: center; background: rgba(0,0,0,.18); color: #fff; font-size: 12px; border-radius: 12px; padding: 3px 10px; margin: 4px 0; }
.sys { align-self: center; font-size: 11px; color: #6d7a86; background: rgba(255,255,255,.65); border-radius: 10px; padding: 3px 9px; }
.row { display: flex; }
.row.user { justify-content: flex-end; }
.row.bot { justify-content: flex-start; }
.bubble { max-width: 82%; border-radius: 14px; padding: 6px 10px; font-size: 15px; line-height: 1.32;
  box-shadow: 0 1px 1px rgba(0,0,0,.10); word-break: break-word; overflow-wrap: anywhere; }
.row.bot .bubble { background: #fff; border-top-left-radius: 5px; }
.row.user .bubble { background: #e1ffc7; border-top-right-radius: 5px; }
.bubble .time { display: block; text-align: right; font-size: 11px; color: #8a9aa8; margin-top: 2px; }
.bubble b { font-weight: 600; }
.bubble code { font-family: ui-monospace, Menlo, monospace; background: #e9edf1; border-radius: 4px; padding: 0 4px; font-size: 13.5px; }
.bubble blockquote { border-left: 3px solid #a7b4c0; margin: 3px 0 3px 4px; padding: 1px 0 1px 8px; display: block; }
.ikb { display: flex; flex-direction: column; gap: 4px; margin-top: 5px; }
.ikb .ikrow { display: flex; gap: 4px; }
.ikb button { flex: 1; background: #f2f3f5; border: none; border-radius: 8px; padding: 8px 6px; font-size: 14.5px; color: #3390ec; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rkb { background: #f1f1f4; border-top: 1px solid #d4d8dd; padding: 8px 6px 12px; display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }
.rkb .rrow { display: flex; gap: 6px; }
.rkb button { flex: 1; background: #fdfdfd; border: none; border-radius: 8px; min-height: 40px; font-size: 15px; color: #1c1c1e; box-shadow: 0 1px 1px rgba(0,0,0,.08); }
.toast { align-self: center; background: #333; color: #fff; border-radius: 14px; font-size: 13px; padding: 7px 14px; }
.caption { font-size: 12px; color: #5c6b78; background: #fff; border: 1px dashed #b9c4cd; border-radius: 8px; padding: 6px 10px; margin: 6px 10px 0; }
"""


def _esc(s):
    return html.escape(s or "")


def _render_msg(m, is_toast=False):
    text = m.get("text") or ""
    kb_type, kb = m.get("kb_type", "none"), m.get("kb", [])
    if is_toast and not kb and len(text) < 60 and "<" not in text:
        return f'<div class="row" style="justify-content:center"><div class="toast">{_esc(text)}</div></div>'
    inner = text.replace("\n", "<br>")
    if kb_type == "inline" and kb:
        rows = "".join('<div class="ikrow">' + "".join(f"<button>{_esc(b)}</button>" for b in r) + "</div>" for r in kb)
        inner += f'<div class="ikb">{rows}</div>'
        if not text.strip():
            inner = f'<span style="display:block;width:60px;height:1px"></span>{inner}'
    return f'<div class="row bot"><div class="bubble">{inner}<span class="time">12:00</span></div></div>'


def render(screens: list[dict]) -> None:
    for s in screens:
        parts = [
            '<div class="tg-header"><div class="name">Личный 🤖</div><div class="status">бот</div></div>',
            '<div class="chat">', '<div class="day">Сегодня</div>',
        ]
        if s.get("trigger"):
            parts.append(f'<div class="sys">▶ {_esc(s["trigger"])}</div>')
        if s.get("user_msg"):
            parts.append(f'<div class="row user"><div class="bubble">{_esc(s["user_msg"])}<span class="time">12:00 ✓✓</span></div></div>')
        reply_kb = None
        for i, m in enumerate(s["msgs"]):
            is_toast = (
                m.get("kb_type") in ("none", "")
                and len(m.get("text") or "") < 60
                and "<" not in (m.get("text") or "")
                and not m.get("kb")
                and i < len(s["msgs"]) - 1
            )
            if m.get("kb_type") == "reply":
                reply_kb = m.get("kb") or []
            parts.append(_render_msg(m, is_toast=is_toast and not s.get("user_msg")))
        if reply_kb:
            rows = "".join('<div class="rrow">' + "".join(f"<button>{_esc(b)}</button>" for b in r) + "</div>" for r in reply_kb)
            parts.append(f'<div class="rkb">{rows}</div>')
        parts.append("</div>")
        body = "".join(parts)
        page = (
            f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{s["id"]}</title>'
            f"<style>{CSS}</style></head><body>"
            f'<div class="caption">Экран {s["id"]} — {_esc(s["title"])}</div>{body}</body></html>'
        )
        with open(os.path.join(PAGES, f'{s["id"]}.html'), "w", encoding="utf-8") as f:
            f.write(page)
    log(f"[render] {len(screens)} страниц → {PAGES}")


# ---------------------------------------------------------------- 4+5. http + shots
def _run_pw(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", PWCLI, *args], capture_output=True, text=True, timeout=120)


def make_shots(screen_ids: list[str]) -> None:
    if not os.path.isfile(PWCLI):
        raise SystemExit(f"[shots] FAIL: нет playwright-cli обёртки: {PWCLI}")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=STATE)
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # wait-ready на первой реальной странице
        for _ in range(40):
            try:
                import urllib.request
                urllib.request.urlopen(f"http://127.0.0.1:{port}/pages/{screen_ids[0]}.html", timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("[shots] FAIL: http-сервер не поднялся")
        _run_pw(["open", "http://127.0.0.1:%d/pages/%s.html" % (port, screen_ids[0])])
        _run_pw(["resize", str(VIEWPORT_W), str(VIEWPORT_H)])
        for sid in screen_ids:
            r_open = _run_pw(["open", f"http://127.0.0.1:{port}/pages/{sid}.html"])
            if r_open.returncode != 0:
                raise SystemExit(f"[shots] FAIL open {sid}: {r_open.stderr.strip()}")
            out = os.path.join(SHOTS, f"{sid}.png")
            r_shot = _run_pw(["screenshot", "--filename", out, "--full-page"])
            if r_shot.returncode != 0 or not os.path.isfile(out):
                raise SystemExit(f"[shots] FAIL screenshot {sid}: {r_shot.stderr.strip()}")
        log(f"[shots] {len(screen_ids)} PNG → {SHOTS}")
    finally:
        httpd.shutdown()
        httpd.server_close()


def _png_width(path: str) -> int:
    r = subprocess.run(["sips", "-g", "pixelWidth", path], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "pixelWidth" in line:
            try:
                return int(line.split(":")[1].strip())
            except (IndexError, ValueError):
                return 0
    return 0


def validate(screen_ids: list[str]) -> None:
    bad = []
    for sid in screen_ids:
        p = os.path.join(SHOTS, f"{sid}.png")
        if not os.path.isfile(p):
            bad.append(f"{sid}: нет файла")
            continue
        size = os.path.getsize(p)
        w = _png_width(p)
        if size < MIN_PNG_BYTES or w < MIN_PNG_W:
            bad.append(f"{sid}: {size}B width={w}")
    if bad:
        raise SystemExit("[validate] FAIL blank/маленькие PNG: " + "; ".join(bad))
    log(f"[validate] OK: все {len(screen_ids)} PNG ≥ {MIN_PNG_BYTES}B и шириной ≥ {MIN_PNG_W}px")


# ---------------------------------------------------------------- 6. report
def write_report(screens: list[dict], shots_ok: bool) -> None:
    lines = [
        "# Visual QA — @igorkyz_bot",
        "",
        f"Конвейер: `qa/run_qa.py` · {len(screens)} экранов · viewport {VIEWPORT_W}px (Telegram iOS).",
        f"Скриншоты: {'✅ все валидны' if shots_ok else '⚠️ не снимались (--no-shots)'} → `qa/state/shots/`.",
        "",
        "| Экран | Триггер | Сообщений | Кнопки |",
        "|-------|---------|-----------|--------|",
    ]
    for s in screens:
        kb_types = sorted({m.get("kb_type", "none") for m in s["msgs"] if m.get("kb")})
        lines.append(
            f"| {s['id']} | {s.get('trigger') or s.get('user_msg') or '—'} | {len(s['msgs'])} | {', '.join(kb_types) or '—'} |"
        )
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"[report] {REPORT}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-shots", action="store_true", help="только screens.json + pages, без браузера")
    args = ap.parse_args()

    setup_state()
    screens = capture()
    render(screens)
    ids = [s["id"] for s in screens]
    if args.no_shots:
        write_report(screens, shots_ok=False)
        log("\nQA (без скриншотов) OK")
        return
    make_shots(ids)
    validate(ids)
    write_report(screens, shots_ok=True)
    log(f"\nQA OK: {len(screens)} экранов, 0 blank, 0 ошибок → {SHOTS}")


if __name__ == "__main__":
    main()
