"""deploy_notes.py — деплой бота голосовых заметок на сервер.

Использование:
    export NOTES_SSH_PASSWORD='...'   # пароль root от сервера
    python deploy_notes.py --init     # первичная установка (единожды)
    python deploy_notes.py            # обычный деплой изменений + рестарт

Сервер: 92.246.128.94 (тот же, что у kaskad/hairos).
"""
import os
import sys
import time

import paramiko

SERVER = os.environ.get("NOTES_SSH_HOST", "92.246.128.94")
USER = os.environ.get("NOTES_SSH_USER", "root")
PASS = os.environ.get("NOTES_SSH_PASSWORD", "")
LOCAL = os.path.dirname(os.path.abspath(__file__))
REMOTE = "/opt/notes-bot"

# Файлы, которые заливаем при каждом деплое
UPLOAD = [
    "app/__init__.py",
    "app/bot.py",
    "app/main.py",
    "app/config.py",
    "app/ai_service.py",
    "app/audio_service.py",
    "app/transcribe_service.py",
    "app/download_service.py",
    "app/reminders.py",
    "app/finance.py",
    "app/tasks.py",
    "app/ui.py",
    "app/tts.py",
    "app/kv.py",
    "requirements.txt",
    ".env.example",
    "README.md",
]


def run(ssh, cmd, label=None):
    _, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if label:
        print(f"  {label}: {'OK' if not err.strip() else err.strip()}")
    return out, err


def main():
    if not PASS:
        print("Ошибка: задай пароль через переменную окружения NOTES_SSH_PASSWORD")
        sys.exit(1)

    do_init = "--init" in sys.argv

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"Подключение к {SERVER}...")
    ssh.connect(SERVER, username=USER, password=PASS, timeout=15)

    if do_init:
        print("=== Первичная установка ===")
        run(ssh, f"mkdir -p {REMOTE}/notes {REMOTE}/downloads", "Создание каталогов")
        run(ssh, "apt-get update -qq && which ffmpeg >/dev/null || apt-get install -y -qq ffmpeg", "ffmpeg")
        run(ssh, f"test -d {REMOTE}/venv || python3 -m venv {REMOTE}/venv", "venv")
        # .env кладём вручную один раз — если его ещё нет, предупреждаем
        out, _ = run(ssh, f"test -f {REMOTE}/.env && echo exists")
        if "exists" not in out:
            print(f"  ⚠️  Положи {REMOTE}/.env вручную (см. .env.example) и chmod 600")

    print("=== Загрузка файлов ===")
    sftp = ssh.open_sftp()
    try:
        run(ssh, f"mkdir -p {REMOTE}/app")
        for rel in UPLOAD:
            local_path = os.path.join(LOCAL, rel)
            if not os.path.exists(local_path):
                print(f"  пропущен (нет локально): {rel}")
                continue
            remote_path = f"{REMOTE}/{rel}"
            if "/" in rel:
                remote_dir = os.path.dirname(remote_path)
                try:
                    sftp.stat(remote_dir)
                except FileNotFoundError:
                    sftp.mkdir(remote_dir)
            sftp.put(local_path, remote_path)
            print(f"  {rel}")
        # юнит всегда актуален
        unit_local = os.path.join(LOCAL, "notes-bot.service")
        sftp.put(unit_local, f"{REMOTE}/notes-bot.service")
        print("  notes-bot.service")
    finally:
        sftp.close()

    if do_init:
        run(ssh, f"{REMOTE}/venv/bin/pip install -q -r {REMOTE}/requirements.txt", "pip install")
        run(ssh, f"cp {REMOTE}/notes-bot.service /etc/systemd/system/notes-bot.service", "Установка юнита")
        run(ssh, "systemctl daemon-reload", "daemon-reload")
        run(ssh, "systemctl enable notes-bot", "enable")

    print("=== Зависимости ===")
    run(ssh, f"{REMOTE}/venv/bin/pip install -q -r {REMOTE}/requirements.txt", "pip install")

    print("=== Рестарт ===")
    run(ssh, "systemctl restart notes-bot", "restart")
    time.sleep(3)
    out, _ = run(ssh, "systemctl is-active notes-bot")
    print(f"  статус: {out.strip()}")

    print("=== Логи (последние 15 строк) ===")
    out, _ = run(ssh, "journalctl -u notes-bot -n 15 --no-pager")
    print(out)

    ssh.close()


if __name__ == "__main__":
    main()
