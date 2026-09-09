#!/usr/bin/env bash
set -Eeuo pipefail

echo "==> Поиск Python 3.12..."

PYTHON_BIN=""
if command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="python3.12"
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; exit(0 if sys.version_info[:2] == (3, 12) else 1)' 2>/dev/null; then
    PYTHON_BIN="python3"
fi

if [[ -z "$PYTHON_BIN" ]]; then
    echo "[-] Ошибка: Python 3.12 не найден." >&2
    echo "    В Arch Linux установите пакет через AUR (например: yay -S python312) или официальные репозитории." >&2
    exit 1
fi

echo "[+] Используется: $($PYTHON_BIN --version)"

VENV_DIR=".venv"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "==> Создание виртуального окружения в '$VENV_DIR'..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    echo "[*] Окружение '$VENV_DIR' уже существует."
fi

echo "==> Установка зависимостей из requirements.txt..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r requirements.txt

# Создание скрипта быстрого запуска
cat << 'EOF' > run.sh
#!/usr/bin/env bash
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/main.py" "$@"
EOF

chmod +x run.sh

echo "==> Установка завершена."
echo "    Для запуска используйте: ./run.sh"
