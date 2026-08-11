"""Где лежит рабочее состояние модерации.

То же решение и по тем же причинам, что apps/twitch-bots/bot/paths.py —
состояние отдельно от кода. Здесь оно особенно заметно: mod.<id>.db
заводится по файлу на канал, и в корне проекта их накапливалось столько
же, сколько каналов, вперемешку с исходниками движка.

Каталоги var/twitch-bots и var/cigilbot разделены по владельцу намеренно,
хотя процесс панели пишет в оба. Причина в именах: registry.db есть и там,
и там — это две независимые БД (источник правды у бота, зеркало здесь, см.
CLAUDE.md), и сложив их в одну папку пришлось бы одну из них переименовать,
потеряв ровно то соответствие имён, по которому их сейчас узнают.
"""

from __future__ import annotations

from pathlib import Path

# cigilbot/paths.py -> cigilbot -> apps/cigilbot -> apps -> корень репозитория
REPO_ROOT = Path(__file__).resolve().parents[3]

VAR = REPO_ROOT / "var" / "cigilbot"

LOGS = VAR / "logs"
RUN = VAR / "run"

# Channel Registry — зеркало реестра каналов из twitch-bots.
REGISTRY_DB = VAR / "registry.db"

# Роли панели (mod_panel_users) — единственная таблица этой БД. Один список
# ADMIN на оба экрана панели (см. apps/panel/panel/auth.py).
MOD_DB = VAR / "mod.db"


def mod_db(broadcaster_id: str) -> Path:
    """mod.<broadcaster_id>.db — всё состояние модерации канала.

    Файл на канал, потому что движок стейтфул и смешивать состояние разных
    каналов незачем. Ключ — broadcaster_id, стабильный к переименованию
    канала, в отличие от login."""
    return VAR / f"mod.{broadcaster_id}.db"


def bot_db(bot_project_root: Path) -> Path:
    """bot.db соседнего проекта — единственный чужой файл, который читает
    Cigilbot (очередь mod_inbox, см. inbox.py).

    Считается от каталога ПРОЕКТА, а не от REPO_ROOT, чтобы переменная
    BOT_PROJECT_ROOT продолжала работать: она существует ровно затем, чтобы
    можно было указать на twitch-bots в другом месте, не правя код.
    apps/twitch-bots -> apps -> корень того репозитория -> var/twitch-bots.

    Один общий bot.db на все каналы (не bot.<instance>.db): один
    бот-аккаунт обслуживает все каналы сразу, инстансов больше нет."""
    return bot_project_root.parent.parent / "var" / "twitch-bots" / "bot.db"


def ensure_dirs() -> None:
    for path in (VAR, LOGS, RUN):
        path.mkdir(parents=True, exist_ok=True)
