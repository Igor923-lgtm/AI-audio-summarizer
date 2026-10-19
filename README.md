# Бот голосовых заметок 🎙️📝

Telegram-бот: наговариваешь голосовую заметку → получаешь структурированный markdown
(самое важное, детали, задачи) + `.md`-файл. Все заметки копятся на сервере в `notes/`.

- **Распознавание:** Groq Whisper (whisper-large-v3)
- **Структурирование:** gpt-oss-120b (Groq)
- **Стек:** Python 3.10+, Aiogram 3, long polling

## Настройка

1. Бот-токен: [@BotFather](https://t.me/botfather) → `/newbot`
2. Ключ Groq: [console.groq.com](https://console.groq.com) → API Keys
3. Свой Telegram ID: [@userinfobot](https://t.me/userinfobot)
4. Заполни `.env` (по образцу `.env.example`):

```
TELEGRAM_BOT_TOKEN=...
GROQ_API_KEY=...
ALLOWED_USER_IDS=123456789
```

## Локальный запуск

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # нужен ffmpeg: brew install ffmpeg
python -m app.main
```

## Команды бота

| Команда | Что делает |
|---|---|
| голосовое / аудиофайл | распознаёт, структурирует, сохраняет в `notes/`, присылает `.md` |
| `/last [N]` | прислать последние N заметок (по умолчанию 3) |
| `/find слово` | поиск по заметкам |

## Деплой на сервер

```bash
export NOTES_SSH_PASSWORD='...'
python deploy_notes.py --init    # первый раз: venv, ffmpeg, systemd, enable
python deploy_notes.py           # дальше: только загрузка изменений + рестарт
```

После `--init` положи на сервер `/opt/notes-bot/.env` (chmod 600) и ещё раз запусти
`python deploy_notes.py`.

Юнит: `/etc/systemd/system/notes-bot.service` (autostart + рестарт при падении).
Заметки: `/opt/notes-bot/notes/`. Логи: `journalctl -u notes-bot -f`.

## Формат заметки

```markdown
# Заголовок по теме

## Самое важное
- 3–5 ключевых тезисов

## Детали
### Подтема
...

## Задачи
- [ ] ... (если есть)
```
