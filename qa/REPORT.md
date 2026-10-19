# Visual QA — @igorkyz_bot

Конвейер: `qa/run_qa.py` · 32 экранов · viewport 390px (Telegram iOS).
Скриншоты: ✅ все валидны → `qa/state/shots/`.

| Экран | Триггер | Сообщений | Кнопки |
|-------|---------|-----------|--------|
| S01 | /start | 1 | reply |
| S02 | /help | 1 | inline |
| S02a | — | 2 | inline |
| S02b | /help | 1 | inline |
| S02c | — | 2 | inline |
| S05 | 📝 Заметки | 1 | inline |
| S06 | ⏰ Напоминания | 1 | inline |
| S07 | ✅ Задачи | 1 | inline |
| S08 | 💸 Финансы | 1 | inline |
| S09 | ui:menu | 1 | reply |
| S11 | ui:notes:next:0 | 1 | inline |
| S11b | ui:notes:next:1 | 1 | inline |
| S15 | ui:notes:search | 1 | — |
| S16 | ui:rem:all | 3 | inline |
| S17 | ui:rem:today | 1 | — |
| S19 | ui:rem:help | 1 | — |
| S18 | ui:rem:all | 1 | — |
| S23 | ui:tasks:active | 1 | inline |
| S25 | ui:tasks:done | 1 | inline |
| S24 | ui:tasks:active | 1 | — |
| S26 | ui:tasks:done | 1 | — |
| S29 | ui:fin:balance | 1 | — |
| S30 | ui:fin:report | 1 | — |
| S32 | ui:fin:list | 1 | — |
| S31 | ui:fin:report | 1 | — |
| S33 | ui:fin:list | 1 | — |
| S43 | напомни завтра в 15:00 позвонить врачу | 1 | inline |
| S44 | задача: сдать отчёт до пятницы | 1 | inline |
| S45 | потратил 500 на такси | 1 | — |
| S46 | привет, расскажи шутку | 1 | — |
| PUSH01 | cron: due | 1 | inline |
| PUSH02 | cron: digest | 1 | reply |
