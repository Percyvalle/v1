"""Тесты на то, что появилось именно при слиянии двух панелей в одну.

Остальное покрытие панели лежит в test_auth.py и test_moderation_api.py и
слияния не касается. Здесь — три вещи, которых до него не существовало:

  * _list_env_profile_channels — чтение каналов из .env/.env.<profile>.
    Раньше эта логика жила в копии auth.py на стороне twitch-bots и
    исчезала, когда обе копии сводили в одну; теперь это половина
    объединённого источника каналов.
  * _list_profile_channels — объединение двух моделей каналов (Registry +
    профили). До слияния каждая копия знала ровно одну модель.
  * _safe_next — возврат на исходный экран после входа. Экранов стало два
    на одном порту, и жёсткий редирект на /moderation выкидывал бы с
    экрана ботов того, кто входил именно туда.

Тут же и регрессия на файл tests/__init__.py: без него pytest импортирует
каталог tests/panel как топ-левел пакет `panel` и затеняет настоящий пакет
панели — тесты падают на ModuleNotFoundError ещё на сборе.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cigilbot.registry_store import RegistryStore
from panel.auth import (
    DEFAULT_AFTER_LOGIN,
    _list_env_profile_channels,
    _list_profile_channels,
    _safe_next,
)
from panel.paths import PanelRoots


class TestEnvProfileChannels:
    def test_main_profile_read_from_repo_root_env(self, tmp_path: Path) -> None:
        # Профиль "main" живёт в КОРНЕВОМ .env монорепо, а не в
        # apps/twitch-bots/.env — общий конфиг после слияния один на репо.
        repo = tmp_path / "repo"
        bot = tmp_path / "bot"
        repo.mkdir()
        bot.mkdir()
        (repo / ".env").write_text("TWITCH_CHANNEL=streamer\n", encoding="utf-8")

        roots = PanelRoots(
            repo=repo, bot=bot, cigilbot=tmp_path,
            bot_var=tmp_path, cigilbot_var=tmp_path, var=tmp_path,
        )
        assert _list_env_profile_channels(roots) == {"main": "streamer"}

    def test_named_profiles_read_from_bot_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        bot = tmp_path / "bot"
        repo.mkdir()
        bot.mkdir()
        (repo / ".env").write_text("TWITCH_CHANNEL=main_channel\n", encoding="utf-8")
        (bot / ".env.second").write_text("TWITCH_CHANNEL=#SecondChannel\n", encoding="utf-8")

        roots = PanelRoots(
            repo=repo, bot=bot, cigilbot=tmp_path,
            bot_var=tmp_path, cigilbot_var=tmp_path, var=tmp_path,
        )
        # Канал нормализуется (без #, нижний регистр) — как и везде в auth.py.
        assert _list_env_profile_channels(roots) == {
            "main": "main_channel",
            "second": "secondchannel",
        }

    def test_example_file_and_channelless_profiles_skipped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        bot = tmp_path / "bot"
        repo.mkdir()
        bot.mkdir()
        (repo / ".env").write_text("TWITCH_CHANNEL=main_channel\n", encoding="utf-8")
        # Шаблон — не профиль.
        (bot / ".env.example").write_text("TWITCH_CHANNEL=твой_канал\n", encoding="utf-8")
        # Профиль с пустым каналом: заведён, но канал ещё не выбран —
        # роль по нему считать не по чему.
        (bot / ".env.blank").write_text("TWITCH_CHANNEL=\n", encoding="utf-8")

        roots = PanelRoots(
            repo=repo, bot=bot, cigilbot=tmp_path,
            bot_var=tmp_path, cigilbot_var=tmp_path, var=tmp_path,
        )
        assert _list_env_profile_channels(roots) == {"main": "main_channel"}

    def test_missing_env_files_are_not_an_error(self, tmp_path: Path) -> None:
        # Свежий клон без .env вообще: панель должна подниматься, а не падать.
        roots = PanelRoots.all_at(tmp_path)
        assert _list_env_profile_channels(roots) == {}


class TestProfileChannelsUnion:
    async def test_union_of_registry_and_env_profiles(self, tmp_path: Path) -> None:
        """Объединение, а не выбор одной модели: role_for_profile получает
        ключ и от moderation_api (broadcaster_id), и от экрана ботов (имя
        профиля). До слияния каждая копия auth.py знала ровно одну модель."""
        (tmp_path / ".env").write_text("TWITCH_CHANNEL=env_channel\n", encoding="utf-8")

        registry = RegistryStore(str(tmp_path / "registry.db"))
        await registry.connect()
        await registry.upsert_channel(
            broadcaster_id="12345", login="registry_channel", registered_by="manual"
        )
        await registry.close()

        roots = PanelRoots.all_at(tmp_path)
        assert await _list_profile_channels(roots) == {
            "main": "env_channel",
            "12345": "registry_channel",
        }

    async def test_registry_wins_on_key_collision(self, tmp_path: Path) -> None:
        """Если имя профиля совпало с broadcaster_id, права определяет
        модель модерации — не .env-файл, который правится руками."""
        (tmp_path / ".env.12345").write_text("TWITCH_CHANNEL=from_env\n", encoding="utf-8")

        registry = RegistryStore(str(tmp_path / "registry.db"))
        await registry.connect()
        await registry.upsert_channel(
            broadcaster_id="12345", login="from_registry", registered_by="manual"
        )
        await registry.close()

        roots = PanelRoots.all_at(tmp_path)
        assert (await _list_profile_channels(roots))["12345"] == "from_registry"


class TestSafeNext:
    @pytest.mark.parametrize("path", ["/bots", "/moderation", "/"])
    def test_internal_paths_allowed(self, path: str) -> None:
        assert _safe_next(path) == path

    @pytest.mark.parametrize(
        "hostile",
        [
            "//evil.com",  # protocol-relative — браузер уведёт на чужой хост
            "https://evil.com",
            "http://evil.com/bots",
            "javascript:alert(1)",
            "",
        ],
    )
    def test_external_targets_fall_back_to_default(self, hostile: str) -> None:
        assert _safe_next(hostile) == DEFAULT_AFTER_LOGIN


class TestPanelRoots:
    def test_default_roots_point_at_real_projects(self) -> None:
        """Панель переехала в apps/panel, и корни перестали совпадать с
        каталогом кода — если эти пути разъедутся, .env и БД будут
        читаться не оттуда, куда пишутся (см. panel/paths.py)."""
        roots = PanelRoots.default()
        assert (roots.bot / "main.py").exists()
        assert (roots.cigilbot / "cigilbot").is_dir()
        assert roots.repo == roots.bot.parent.parent

    def test_state_lives_outside_the_source_trees(self) -> None:
        """Ровно то, ради чего заведён var/: рабочее состояние не внутри
        каталогов с исходниками. Если кто-то вернёт БД обратно в проект,
        сломается это утверждение, а не только вкус."""
        roots = PanelRoots.default()
        assert roots.bot_var == roots.repo / "var" / "twitch-bots"
        assert roots.cigilbot_var == roots.repo / "var" / "cigilbot"
        for var_dir in (roots.bot_var, roots.cigilbot_var):
            assert not var_dir.is_relative_to(roots.bot)
            assert not var_dir.is_relative_to(roots.cigilbot)

    def test_panel_and_engines_agree_on_state_dirs(self) -> None:
        """panel/paths.py дублирует определения из bot/paths.py и
        cigilbot/paths.py (импортировать их оттуда мешает порядок
        sys.path-бутстрапа). Дубль обязан совпадать: разъедется — панель
        будет писать в один файл, а движок читать другой."""
        from bot import paths as bot_paths
        from cigilbot import paths as cigilbot_paths

        roots = PanelRoots.default()
        assert roots.bot_var == bot_paths.VAR
        assert roots.cigilbot_var == cigilbot_paths.VAR
        assert roots.repo == bot_paths.REPO_ROOT == cigilbot_paths.REPO_ROOT

    def test_registry_is_one_database_for_everyone(self) -> None:
        """Реестров было два — свой у бота и зеркало у модерации, которые
        синхронизировала панель. Пока это были разные процессы, зеркало
        имело смысл; теперь оба движка в одном процессе, и расхождение
        двух копий стало бы багом внутри него. Все трое обязаны смотреть
        в один файл, и он вне каталога любого из движков."""
        from bot import paths as bot_paths
        from cigilbot import paths as cigilbot_paths

        roots = PanelRoots.default()
        assert roots.registry_db == bot_paths.REGISTRY_DB == cigilbot_paths.REGISTRY_DB
        assert roots.registry_db == roots.repo / "var" / "registry.db"
        assert not roots.registry_db.is_relative_to(roots.bot_var)
        assert not roots.registry_db.is_relative_to(roots.cigilbot_var)
