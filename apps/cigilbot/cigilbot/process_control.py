"""Управление consumer-процессами (subprocess.Popen + pid-файл + tasklist).

Тот же проверенный приём, что twitch-bots/panel/server.py использует для
управления main.py (см. _pid_file/_read_pid/_process_alive/_stop_pid/_start
там) — Windows не даёт надёжного async-API для проверки живости чужого
процесса, поэтому используется tasklist/taskkill через subprocess, как уже
работает в соседнем проекте. Ключ здесь — broadcaster_id (не profile),
согласно решению перейти на стабильный Twitch ID (см.
docs/master-plan.html, направление 00, и registry_migrations.py).

Используется supervisor.py (фоновый луп внутри панели) — не вызывается
напрямую из HTTP-хендлеров: эндпоинты только выставляют desired_state в
registry.db, а supervisor.py на следующем тике решает, нужно ли запускать/
останавливать процесс (см. RegistryStore.desired_state vs process_status).
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).parent.parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

# broadcaster_id всегда приходит из Twitch Helix (числовой ID) — но
# start_consumer()/pid_file() также достижимы из HTTP-пути
# (/api/registry/channels/{broadcaster_id}/start) через supervisor.py,
# который читает его из registry.db. Валидация здесь — защита по
# глубине: не даёт "грязному" значению стать частью имени pid/log-файла
# (path traversal через ../ в теории, если Registry когда-нибудь начнёт
# принимать broadcaster_id не только от Helix).
_BROADCASTER_ID_RE = re.compile(r"^\d+$")


def _validate_broadcaster_id(broadcaster_id: str) -> None:
    if not _BROADCASTER_ID_RE.fullmatch(broadcaster_id):
        raise ValueError(f"Некорректный broadcaster_id: {broadcaster_id!r}")


def pid_file(broadcaster_id: str) -> Path:
    _validate_broadcaster_id(broadcaster_id)
    return ROOT / f"consumer.{broadcaster_id}.pid"


def _lock_file(broadcaster_id: str) -> Path:
    return ROOT / f"consumer.{broadcaster_id}.lock"


# Атомарная секция "проверить pid жив -> записать новый pid" per-channel —
# не глобальный lock, чтобы start/stop разных каналов не блокировали друг
# друга. См. bot_process_control.py::_pid_lock для того же паттерна и
# обоснования (двойной клик/гонка supervisor против ручного start иначе
# может породить два живых consumer'а на один канал).
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_STALE_SECONDS = 30.0


@contextlib.contextmanager
def _pid_lock(broadcaster_id: str) -> Iterator[None]:
    lock_path = _lock_file(broadcaster_id)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.monotonic() - lock_path.stat().st_mtime > _LOCK_STALE_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Не удалось получить lock на управление consumer {broadcaster_id!r} (занято другим запросом)"
                ) from None
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (ValueError, OSError):
        return None


def process_alive(pid: int) -> bool:
    # /FI IMAGENAME защищает от PID reuse false positive: если consumer
    # умер и ОС успела переиспользовать его PID для другого процесса до
    # того, как supervisor это заметил (окно тика — 10с), не считаем его
    # живым consumer'ом только потому что число совпало.
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FI", "IMAGENAME eq python.exe", "/NH"],
        capture_output=True,
        text=True,
    )
    return str(pid) in result.stdout


def is_running(broadcaster_id: str) -> bool:
    pid = read_pid(pid_file(broadcaster_id))
    return bool(pid and process_alive(pid))


def stop_consumer(broadcaster_id: str) -> None:
    with _pid_lock(broadcaster_id):
        path = pid_file(broadcaster_id)
        pid = read_pid(path)
        if pid and process_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
        path.unlink(missing_ok=True)


def start_consumer(broadcaster_id: str) -> int:
    """Запускает `python -m cigilbot.consumer <broadcaster_id>` как
    управляемый subprocess, пишет pid в pid-файл. Логи — в
    logs/consumer.<broadcaster_id>.out.log / .err.log (создаётся при
    необходимости, тот же принцип, что twitch-bots/panel/server.py._start).
    Обёрнуто в _pid_lock() — см. docstring там же."""
    with _pid_lock(broadcaster_id):
        existing = read_pid(pid_file(broadcaster_id))
        if existing is not None and process_alive(existing):
            return existing

        logs_dir = ROOT / "logs"
        logs_dir.mkdir(exist_ok=True)
        log_out = logs_dir / f"consumer.{broadcaster_id}.out.log"
        log_err = logs_dir / f"consumer.{broadcaster_id}.err.log"

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # Дескрипторы закрываются в родителе сразу после Popen — см.
        # обоснование в bot_process_control.py::start_bot.
        with open(log_out, "a", encoding="utf-8") as out_f, open(log_err, "a", encoding="utf-8") as err_f:
            proc = subprocess.Popen(
                [str(VENV_PYTHON), "-m", "cigilbot.consumer", broadcaster_id],
                cwd=str(ROOT),
                stdout=out_f,
                stderr=err_f,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                env=env,
            )
        pid_file(broadcaster_id).write_text(str(proc.pid), encoding="ascii")
        return proc.pid
