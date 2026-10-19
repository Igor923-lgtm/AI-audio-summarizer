import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from app.bot import bot, dp, _fmt_when_msk, _repeat_label, ALLOWED_IDS
from app import reminders as reminders_db
from app import tasks as tasks_db
from app import kv
from app import tts
from app import ui

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)

TZ_MSK = timezone(timedelta(hours=3))


async def send_due_reminders(b: Bot):
    now = datetime.now(timezone.utc)
    for row in reminders_db.get_due(now):
        try:
            rule = row["repeat_rule"]
            # закрываем текущее
            if rule:
                reminders_db.mark_sent(row["id"])
                # пересчитываем следующий срок (row датa в UTC → MSK)
                due_msk = datetime.fromisoformat(row["due_at"]).astimezone(TZ_MSK)
                nxt = reminders_db.next_due(rule, due_msk)
                if nxt:
                    reminders_db.add_reminder(row["chat_id"], row["text"], nxt, rule)
                    extra = _repeat_label(rule)
                else:
                    extra = ""
            else:
                reminders_db.complete(row["id"])
                extra = ""
            await b.send_message(
                row["chat_id"],
                f"⏰ <b>Напоминание</b>\n📌 {ui.esc(row['text'])}\n"
                f"🗓 {_fmt_when_msk(row['due_at'])}{extra}",
                reply_markup=ui.reminder_actions_kb(row["id"]),
            )
            logger.info(f"Напоминание #{row['id']} отправлено: {row['text']}")
        except Exception as e:
            logger.error(f"Не удалось отправить напоминание #{row['id']}: {e}")


async def send_digest_if_needed(b: Bot, chat_id: int):
    """Утренняя сводка в 09:00 MSK, раз в день."""
    now = datetime.now(TZ_MSK)
    if now.hour < 9:
        return
    today_key = now.strftime("%Y-%m-%d")
    state_path = "digest_state.txt"
    try:
        with open(state_path) as f:
            if f.read().strip() == today_key:
                return
    except FileNotFoundError:
        pass

    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    rems = reminders_db.get_today(chat_id, start, end)
    tasks = tasks_db.list_tasks(chat_id, done=False)

    lines = [ui.card("Доброе утро", f"Сегодня {now.strftime('%d.%m')}:", "☀️")]
    if rems:
        lines.append(f"\n⏰ Напоминаний: <b>{len(rems)}</b>")
        for r in rems[:5]:
            lines.append(f"• {ui.esc(r['text'])} — {_fmt_when_msk(r['due_at'])}")
    else:
        lines.append("\n⏰ Напоминаний на сегодня нет")
    if tasks:
        lines.append(f"\n✅ Активных задач: <b>{len(tasks)}</b>")
        for t in tasks[:5]:
            lines.append(f"• {ui.esc(t['text'])}")
    lines.append("\nХорошего дня! 🚀")

    try:
        text = "\n".join(lines)
        await b.send_message(chat_id, text, reply_markup=ui.main_kb())
        if kv.voice_enabled(chat_id):
            audio = await tts.synthesize(
                f"Доброе утро. Напоминаний на сегодня: {len(rems)}. Активных задач: {len(tasks)}.",
                kv.voice_key(chat_id),
            )
            if audio:
                from aiogram import types

                await b.send_voice(chat_id, types.BufferedInputFile(audio, filename="digest.mp3"))
        with open(state_path, "w") as f:
            f.write(today_key)
    except Exception as e:
        logger.error(f"Не удалось отправить сводку: {e}")


async def reminder_loop(b: Bot):
    while True:
        try:
            await send_due_reminders(b)
            for uid in ALLOWED_IDS:
                await send_digest_if_needed(b, uid)
        except Exception as e:
            logger.error(f"Ошибка цикла напоминаний: {e}")
        await asyncio.sleep(20)


async def main():
    logger.info("Запуск бота (long polling)...")
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot, drop_pending_updates=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
