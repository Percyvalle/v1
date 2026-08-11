"""Корни, на которые смотрит объединённая панель.

До слияния каждая панель жила внутри своего проекта и обходилась одним
`ROOT = Path(__file__).parent.parent` — он же был корнем проекта, он же
местом .env, он же местом БД. Панель переехала в apps/panel/, и это
совпадение развалилось: код лежит здесь, а состояние — в двух соседних
проектах. Поэтому корней теперь три, и путать их нельзя.

REPO_ROOT — единственный .env на весь монорепо. Раньше их было два, с
разными значениями одних и тех же ключей (PANEL_TWITCH_CLIENT_ID/SECRET,
PANEL_SESSION_SECRET, PANEL_TWITCH_CHANNEL), потому что двум процессам на
разных портах нужны были разные redirect URI. Панель одна — приложение
Twitch тоже одно, и дублировать ключи больше незачем.

Здесь же лежит важная связка: panel/auth.py::_write_env_values пишет в
этот файл TWITCH_MOD_* после входа под аккаунтом бота, а
cigilbot/consumer.py читает их оттуда же. Если эти два пути разойдутся,
токен молча запишется не туда, откуда читается, и все баны начнут падать
с 401 — при этом панель будет показывать, что токен получен.

CIGILBOT_ROOT — registry.db, mod.db, mod.<broadcaster_id>.db, config/.
BOT_ROOT — bot.db, .env.<profile> (профильная модель осталась как была),
prompts/, main.py, который панель запускает как subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# apps/panel/panel/paths.py -> panel -> apps/panel -> apps -> корень репо
REPO_ROOT = Path(__file__).resolve().parents[3]

BOT_ROOT = REPO_ROOT / "apps" / "twitch-bots"
CIGILBOT_ROOT = REPO_ROOT / "apps" / "cigilbot"

# Рабочее состояние — отдельно от исходников, по каталогу на владельца.
# Определения дублируются в bot/paths.py и cigilbot/paths.py: панель не
# может импортировать их оттуда раньше, чем отработает sys.path-бутстрап в
# panel/__init__.py, а сам бутстрап импортирует этот модуль. Три строки
# дубля дешевле, чем ленивый импорт в каждой точке использования.
BOT_VAR = REPO_ROOT / "var" / "twitch-bots"
CIGILBOT_VAR = REPO_ROOT / "var" / "cigilbot"

# Единственный .env монорепо (см. докстринг выше про связку с consumer.py).
ENV_FILE = REPO_ROOT / ".env"

# Профиль бота по умолчанию: живёт в корневом .env, а не в .env.<profile>.
# Определён здесь, а не в bots_api.py, потому что нужен ещё и auth.py при
# сборе списка каналов для по-канальных ролей — раньше эти два места были
# в разных процессах и каждое держало свою копию константы.
MAIN_PROFILE = "main"

# Общий venv в корне — один на оба проекта и панель. Панель запускает
# main.py и consumer.py как subprocess, и обоим нужен именно он: панель
# импортирует и bot.*, и cigilbot.*, так что раздельные venv больше не
# собираются в один процесс (см. корневой requirements.txt).
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"


@dataclass(frozen=True, slots=True)
class PanelRoots:
    """Три корня одной пачкой — то, что панель кладёт в app.state.

    Раньше в app.state лежал один `panel_root`, и этого хватало: панель
    жила внутри своего проекта, где .env и БД лежали в одной папке. После
    переезда в apps/panel/ хранить один путь стало нельзя, а хранить три
    отдельных ключа — значит каждый раз гадать, какой из них имел в виду
    вызывающий код. Отсюда явная тройка с говорящими именами.

    В тестах все три обычно указывают на одну tmp-папку (см.
    tests/panel/conftest.py) — это законно и именно так и было до переезда.
    """

    repo: Path
    """Где лежит единственный .env: PANEL_TWITCH_*, TWITCH_MOD_*."""

    bot: Path
    """apps/twitch-bots: .env.<profile>, main.py, prompts/ — исходники."""

    cigilbot: Path
    """apps/cigilbot: движок модерации и config/ — исходники."""

    bot_var: Path
    """var/twitch-bots: bot.db, registry.db, usage.json, логи, pid."""

    cigilbot_var: Path
    """var/cigilbot: registry.db, mod.db, mod.<broadcaster_id>.db, логи, pid."""

    @classmethod
    def default(cls) -> PanelRoots:
        return cls(
            repo=REPO_ROOT,
            bot=BOT_ROOT,
            cigilbot=CIGILBOT_ROOT,
            bot_var=BOT_VAR,
            cigilbot_var=CIGILBOT_VAR,
        )

    @classmethod
    def all_at(cls, path: Path) -> PanelRoots:
        """Все корни в одной папке — для тестов."""
        return cls(repo=path, bot=path, cigilbot=path, bot_var=path, cigilbot_var=path)
