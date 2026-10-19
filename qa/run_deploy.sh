#!/usr/bin/env bash
# Деплой @igorkyz_bot за один проход: тест → compile → deploy → verify.
#   bash qa/run_deploy.sh
# Exit 0 = DEPLOY_OK; иначе DEPLOY_FAIL (fail-fast на первом этапе).
set -euo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT"
VENV="$PROJECT/venv/bin/python"
export DEPLOY_SETTINGS="/Users/igor/mimo/kaskad-multitenant/deploy_settings.py"
STATE="$PROJECT/qa/state"
mkdir -p "$STATE"

say() { printf '\n=== %s ===\n' "$1"; }

say "1/4 Регрессия"
"$VENV" -u "$PROJECT/qa/test_all_buttons.py"
echo "тесты OK"

say "2/4 compileall"
"$VENV" -m compileall -q app/
echo "compile OK"

say "3/4 deploy (пароль парсится из deploy_settings.py, не импортом)"
# deploy_settings.py исполняется при import — парсим строку PASS regex+ast.
PASS="$("$VENV" -c "
import re, ast
t = open('$DEPLOY_SETTINGS').read()
print(ast.literal_eval(re.search(r'^PASS\s*=\s*(.+)$', t, re.M).group(1).strip()))
")"
NOTES_SSH_PASSWORD="$PASS" "$VENV" -u deploy_notes.py 2>&1 | tee "$STATE/deploy.log"
grep -q "статус: active" "$STATE/deploy.log" || { echo "DEPLOY_FAIL: нет 'статус: active' в логе"; exit 1; }

say "4/4 verify на сервере"
"$VENV" -u - <<'PYEOF'
import ast, os, re, sys
import paramiko

t = open(os.environ["DEPLOY_SETTINGS"]).read()
P = ast.literal_eval(re.search(r'^PASS\s*=\s*(.+)$', t, re.M).group(1).strip())
s = paramiko.SSHClient()
s.set_missing_host_key_policy(paramiko.AutoAddPolicy())
s.connect("92.246.128.94", username="root", password=P, timeout=15)

checks = [
    ("status", "systemctl is-active notes-bot", "active"),
    ("_help_text", "grep -c '_help_text' /opt/notes-bot/app/bot.py", None),
    ("_money", "grep -c '_money' /opt/notes-bot/app/bot.py", None),
    ("Отмена", "grep -c 'Отмена' /opt/notes-bot/app/ui.py", None),
]
fails = []
for name, cmd, want in checks:
    _in, out, _err = s.exec_command(cmd, timeout=20)
    val = out.read().decode().strip()
    ok = (val == want) if want is not None else (val.isdigit() and int(val) > 0)
    print(f"  {name} = {val} {'OK' if ok else 'FAIL'}")
    if not ok:
        fails.append(f"{name}={val}")
s.close()
if fails:
    print("DEPLOY_FAIL: " + ", ".join(fails))
    sys.exit(1)
print("\nDEPLOY_OK")
PYEOF

say "Готово"
