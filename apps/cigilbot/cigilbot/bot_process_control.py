"""Управление процессом main.py (чат-бот twitch-bots) напрямую из Cigilbot.

Полностью независимо от twitch-bots/panel/server.py (порт 8765) — та
панель остаётся отдельным продуктом для LLM-ботов (Chat/Brain/Prompt) и
больше не должна быть точкой входа для запуска/остановки multi-channel
бота модерации. Панель модерации (порт 8766, panel/registry_api.py) сама
запускает main.py как subprocess напрямую, тем же проверенным приёмом
(subprocess.Popen + pid-файл + tasklist), что уже работает в
cigilbot/process_control.py (управление consumer-процессами) и в самой
twitch-bots/panel/server.py.

main.py при запуске с BOT_ENV_FILE=.env.cigilbot сам читает список
активных каналов из своего registry.db (см. twitch-bots/main.py::
_load_initial_channels) — здесь ничего про конкретные каналы знать не
нужно, только держать сам процесс живым.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

CIGILBOT_ROOT = Path(__file__).parent.parent

# BOT_PROJECT_ROOT — та же переменная, что уже использует cigilbot/consumer.py
# для доступа к bot.db; здесь она указывает на корень twitch-bots целиком,
# откуда запускается main.py.
BOT_PROJECT_ROOT = Path(
    os.environ.get("BOT_PROJECT_ROOT", str(CIGILBOT_ROOT.parent / "twitch-bots"))
)

# Общий venv в корне монорепо — не .venv внутри twitch-bots, которого больше
# нет (см. корневой requirements.txt). Считается от CIGILBOT_ROOT, а не от
# BOT_PROJECT_ROOT: последний настраивается через переменную окружения и
# может указывать куда угодно, тогда как venv у монорепо ровно один.
BOT_VENV_PYTHON = CIGILBOT_ROOT.parent.parent / ".venv" / "Scripts" / "python.exe"

# Профиль twitch-bots, читающий каналы из Registry (см. .env.cigilbot в
# apps/twitch-bots — единственный профиль без DEEPSEEK_API_KEY, чисто
# модерационный). Не настраивается снаружи: этот модуль управляет ровно
# одним конкретным ботом-процессом, а не произвольным профилем.
BOT_ENV_FILE_NAME = ".env.cigilbot"

PID_FILE = CIGILBOT_ROOT / "chatbot.pid"
LOCK_FILE = CIGILBOT_ROOT / "chatbot.lock"
LOG_OUT = CIGILBOT_ROOT / "logs" / "chatbot.out.log"
LOG_ERR = CIGILBOT_ROOT / "logs" / "chatbot.err.log"

# Между двумя параллельными вызовами start_bot() (например, двойной клик в
# панели, или ручной start во время автоматического) нужна атомарная секция
# "проверить pid жив -> записать новый pid", иначе оба вызова могут пройти
# проверку is_running()==False до того, как первый запишет PID_FILE, и
# породить два живых main.py. Lock-файл через O_CREAT|O_EXCL — атомарен на
# Windows и не требует msvcrt.locking.
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_STALE_SECONDS = 30.0  # защита от вечного зависания, если процесс упал с открытым lock-файлом


@contextlib.contextmanager
def _pid_lock() -> Iterator[None]:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd = None
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.monotonic() - LOCK_FILE.stat().st_mtime > _LOCK_STALE_SECONDS:
                    LOCK_FILE.unlink(missing_ok=True)
                    continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Не удалось получить lock на управление chatbot-процессом (занято другим запросом)"
                ) from None
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(fd)
        LOCK_FILE.unlink(missing_ok=True)


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None


def _process_alive(pid: int) -> bool:
    # /V + сверка имени образа защищает от PID reuse: если исходный
    # main.py умер и ОС успела отдать его PID другому процессу до того,
    # как мы это заметили, "python.exe" в выводе всё ещё может случайно
    # совпасть — но это уже конкретный, а не любой процесс с этим PID.
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FI", "IMAGENAME eq python.exe", "/NH"],
        capture_output=True,
        text=True,
    )
    return str(pid) in result.stdout


def is_running() -> bool:
    pid = _read_pid()
    return bool(pid and _process_alive(pid))


def get_pid() -> int | None:
    pid = _read_pid()
    return pid if pid and _process_alive(pid) else None


def stop_bot() -> None:
    with _pid_lock():
        pid = _read_pid()
        if pid and _process_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
        PID_FILE.unlink(missing_ok=True)


def start_bot() -> int:
    """Не запускает второй раз, если процесс уже жив — возвращает
    существующий pid (идемпотентно, как cigilbot/process_control.py).
    Обёрнуто в _pid_lock(), чтобы параллельный вызов (двойной клик,
    supervisor + ручной start) не мог породить два живых main.py."""
    with _pid_lock():
        existing = get_pid()
        if existing is not None:
            return existing

        if not BOT_VENV_PYTHON.exists():
            raise RuntimeError(
                f"Не найден общий venv монорепо: {BOT_VENV_PYTHON} — "
                f"создайте его в корне: python -m venv .venv && "
                f".\\.venv\\Scripts\\pip install -r requirements.txt"
            )

        LOG_OUT.parent.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["BOT_ENV_FILE"] = str(BOT_PROJECT_ROOT / BOT_ENV_FILE_NAME)

        # Файлы логов открываются на время Popen(...) и сразу закрываются
        # в родителе: Popen дублирует дескриптор дочернему процессу через
        # dup(), исходный объект в родителе нужен только на момент спавна.
        # Без явного close() объект держится живым до GC — при частых
        # рестартах (supervisor) это утечка FD в долгоживущем процессе панели.
        with open(LOG_OUT, "a", encoding="utf-8") as out_f, open(LOG_ERR, "a", encoding="utf-8") as err_f:
            proc = subprocess.Popen(
                [str(BOT_VENV_PYTHON), "main.py"],
                cwd=str(BOT_PROJECT_ROOT),
                stdout=out_f,
                stderr=err_f,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                env={**env, "PYTHONIOENCODING": "utf-8"},
            )
        PID_FILE.write_text(str(proc.pid), encoding="ascii")
        return proc.pid
