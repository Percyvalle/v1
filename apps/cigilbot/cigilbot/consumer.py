"""Фоновый процесс-потребитель Cigilbot: держит единственный долгоживущий
ModerationEngine (стейтфул — скользящее окно, состояния пользователей,
детекция кластеров/рейдов в реальном времени, см. engine.py::observe),
разбирает входящую очередь чата из twitch-bots и исполняет очередь действий
панели (BAN/TIMEOUT/DELETE_MESSAGES) через Twitch Helix.

До разделения на два проекта вся эта логика жила внутри main.py в TWITCH
BOTS (ChatBot._poll_moderation_updates/_poll_action_queue/event_message).
main.py больше не импортирует ModerationEngine — вместо прямого
in-process вызова observe() на каждое сообщение чата, main.py кладёт
сериализованный ChatEvent в mod_inbox (в своей же bot.db), а этот процесс
читает его оттуда в фоне (см. cigilbot/inbox.py) и кормит им движок.
main.py никогда не ждёт ответа и не падает, если этот процесс недоступен —
задания просто накопятся в очереди.

Один Twitch-бот-аккаунт обслуживает ВСЕ каналы (main.py слушает их через
initial_channels списком) — но Cigilbot по-прежнему держит ОДИН
consumer-процесс на канал (движок стейтфул, смешивать состояние разных
каналов не нужно). Канал идентифицируется по broadcaster_id (стабилен к
переименованию), не по .env.<profile> — конфигурация канала читается из
Channel Registry (registry.db), не из файла.

Запуск: .venv\\Scripts\\python -m cigilbot.consumer <broadcaster_id>
Управляется supervisor'ом (cigilbot/supervisor.py) — ручной запуск через
терминал по-прежнему работает для отладки, просто не поддерживается
автоматическим restart-on-crash без supervisor'а.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("cigilbot.consumer")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cigilbot import paths
from cigilbot.config import load_channel_profile, load_config as load_moderation_config
from cigilbot.engine import ModerationEngine
from cigilbot.executor import ActionExecutor, process_pending
from cigilbot.inbox import ChatInbox
from cigilbot.mod_token import ModTokenError, ModTokenManager, load_mod_token_manager
from cigilbot.registry_store import RegistryStore
from cigilbot.store import ModerationStore
from cigilbot.twitch_api import HelixClient
from cigilbot.types import ChatEvent, Mode, RiskLevel

INBOX_POLL_SECONDS = 1.0
STATE_SYNC_SECONDS = 10
ACTION_QUEUE_POLL_SECONDS = 2.0
FP_PENALTY_SYNC_EVERY_N_TICKS = 10
ACCOUNT_AGE_POLL_SECONDS = 5.0
ACCOUNT_AGE_BATCH_SIZE = 100  # лимит get_users() на один Helix-запрос


def _read_env(env_file: Path, key: str) -> str:
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1]
    return ""


def _load_env_file(env_file: Path) -> None:
    """Мини-загрузчик .env без внешней зависимости на профиль — тот же
    формат KEY=VALUE, что main.py в twitch-bots читает через python-dotenv.
    Используется только для корневого .env (PANEL_TWITCH_CLIENT_ID/SECRET,
    BOT_PROJECT_ROOT) — конфигурация канала больше не в .env файлах."""
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value)


class ModerationConsumer:
    def __init__(self, *, broadcaster_id: str, bot_db_path: Path, mod_db_path: Path, channel: str):
        self.broadcaster_id = broadcaster_id
        self.channel = channel
        self.store = ModerationStore(str(mod_db_path))
        self.inbox = ChatInbox(str(bot_db_path), channel_login=channel)
        self.engine: ModerationEngine | None = None
        self.mod_token_manager: ModTokenManager | None = None
        self.helix_client: HelixClient | None = None
        # Отдельный клиент только для get_users() (возраст аккаунта) — та
        # ручка работает по App Access Token (client_id/secret), без scope
        # и без токена модератора, поэтому не завязана на mod_token_manager
        # ниже и доступна даже до того, как токен бота получен через панель.
        self.account_age_client: HelixClient | None = None
        # user_id, которым ещё не резолвили account_created_at (Verdict
        # пришёл is_provisional). Set, не list — одно и то же сообщение от
        # активного чаттера не должно добавлять дубликаты на каждый вердикт.
        self._pending_account_age: set[str] = set()

    async def start(self) -> None:
        await self.store.connect()
        await self.inbox.connect()

        self.engine = ModerationEngine(
            load_moderation_config(),
            load_channel_profile(self.channel),
            self.store,
            mode=Mode.SHADOW,
        )
        await self.engine.reload_patterns()
        await self.engine.sync_attack_mode()
        await self.engine.sync_giveaway_mode()
        await self.engine.reload_fp_penalties()
        log.info(
            "Cigilbot consumer запущен (broadcaster_id=%s, канал=%s, SHADOW)",
            self.broadcaster_id, self.channel,
        )

        # Токен модератора живёт в КОРНЕВОМ .env (не per-profile — одна
        # панель модерации, одна кнопка получения токена; см. panel/auth.py
        # ::/auth/bot/login), как и было в main.py до разделения.
        root_env_file = ROOT / ".env"
        panel_client_id = os.environ.get("PANEL_TWITCH_CLIENT_ID", "")
        panel_client_secret = os.environ.get("PANEL_TWITCH_CLIENT_SECRET", "")
        if panel_client_id and panel_client_secret:
            self.mod_token_manager = load_mod_token_manager(
                client_id=panel_client_id,
                client_secret=panel_client_secret,
                env_file=root_env_file,
            )
        if self.mod_token_manager is not None:
            self.helix_client = HelixClient(panel_client_id, panel_client_secret)
            log.info("Токен модератора настроен — очередь действий будет исполняться")
        else:
            log.info(
                "Токен модератора не настроен — очередь действий из панели "
                "накапливается, но не исполняется (получите токен в Settings панели)"
            )

        if panel_client_id and panel_client_secret:
            self.account_age_client = HelixClient(panel_client_id, panel_client_secret)
        else:
            log.info(
                "PANEL_TWITCH_CLIENT_ID/SECRET не заданы — возраст аккаунта не "
                "резолвится, детектор new_account будет работать только на "
                "is_provisional-эвристиках"
            )

        await asyncio.gather(
            self._poll_inbox(),
            self._poll_state_sync(),
            self._poll_action_queue(),
            self._poll_account_age(),
        )

    async def _poll_inbox(self) -> None:
        """Разбирает mod_inbox (см. cigilbot/inbox.py) — сообщения чата и
        события рейда, которые main.py кладёт туда вместо прямого вызова.
        Порядок ID = порядок появления в чате, поэтому обрабатываем строго
        по возрастанию id, без параллелизма — движок стейтфул (скользящее
        окно), обработка не по порядку исказила бы кластеризацию."""
        assert self.engine is not None
        while True:
            try:
                items = await self.inbox.get_pending(limit=50)
                done_ids: list[int] = []
                for item in items:
                    await self._handle_inbox_item(item.event)
                    done_ids.append(item.id)
                if done_ids:
                    await self.inbox.mark_done_many(done_ids)
            except Exception:
                log.exception("Сбой разбора входящей очереди чата")
            await asyncio.sleep(INBOX_POLL_SECONDS)

    # dict[str, Any], а не dict[str, object]: payload — это разобранный JSON из
    # mod_inbox, где значения заведомо разнотипные (float/str/bool/список), и
    # ChatEvent ниже собирается из них по конкретным полям.
    async def _handle_inbox_item(self, payload: dict[str, Any]) -> None:
        assert self.engine is not None
        kind = payload.get("kind")
        if kind == "raid_started":
            # FALSE-BAN-001 аудита: снижает чувствительность на время рейда
            # (см. engine.py, RAID_CONTEXT_SECONDS) — было main.py
            # ::event_raw_usernotice, теперь приходит через ту же очередь.
            self.engine.mark_raid_started()
            return
        if kind != "chat_message":
            log.warning("Неизвестный тип записи в mod_inbox: %r", kind)
            return

        event = ChatEvent(
            user_id=payload["user_id"],
            login=payload["login"],
            text=payload["text"],
            timestamp=payload["timestamp"],
            channel=payload.get("channel", ""),
            display_name=payload.get("display_name", ""),
            message_id=payload.get("message_id", ""),
            is_first_message=payload.get("is_first_message", False),
            is_returning_chatter=payload.get("is_returning_chatter", False),
            is_subscriber=payload.get("is_subscriber", False),
            is_moderator=payload.get("is_moderator", False),
            is_vip=payload.get("is_vip", False),
            is_broadcaster=payload.get("is_broadcaster", False),
            badges=tuple(payload.get("badges", ())),
        )
        try:
            verdict = await self.engine.observe(event)
        except Exception:
            log.exception("Сбой движка модерации на сообщении из очереди")
            return

        if verdict.is_provisional:
            self._pending_account_age.add(event.user_id)

        if verdict.risk_level != RiskLevel.LOW:
            log.info(
                "[MODERATION][%s] %s risk=%d conf=%.2f action=%s signals=%s",
                verdict.mode.value,
                event.login,
                verdict.risk_score,
                verdict.confidence,
                verdict.recommended_action.value,
                ", ".join(verdict.signal_names),
            )

    async def _poll_state_sync(self) -> None:
        """Было main.py::_poll_moderation_updates. Панель — отдельный
        процесс, меняющий Attack Mode/Pattern Library/feedback через ту же
        mod.<broadcaster_id>.db — движок не видит эти изменения автоматически,
        поэтому перечитывает их периодически. Частоты как в исходнике:
        Attack Mode/Pattern Library каждый тик (10 сек, панический режим
        должен реагировать быстро), fp_penalty реже (копится медленно)."""
        assert self.engine is not None
        tick = 0
        while True:
            try:
                await self.engine.reload_patterns()
                await self.engine.sync_attack_mode()
                await self.engine.sync_giveaway_mode()
                if tick % FP_PENALTY_SYNC_EVERY_N_TICKS == 0:
                    await self.engine.reload_fp_penalties()
            except Exception:
                log.exception("Не удалось обновить состояние модерации из БД")
            tick += 1
            await asyncio.sleep(STATE_SYNC_SECONDS)

    async def _poll_account_age(self) -> None:
        """Резолвит account_created_at для пользователей, чей вердикт пришёл
        is_provisional (см. store.py::set_account_created_at). Без этого
        detectors/account.py::new_account никогда не срабатывает по-настоящему
        — is_provisional остаётся True навсегда, а не только до первого
        ответа Helix, как задумано (см. ChatEvent docstring в types.py).

        Батчит по ACCOUNT_AGE_BATCH_SIZE (лимит get_users() за один запрос)
        вместо запроса на каждого пользователя — при активном чате новых
        зрителей может быть много одновременно, отдельный запрос на каждого
        быстро упёрся бы в rate limit Helix.

        Вызывает engine.update_account_age(), а не store.set_account_created_at()
        напрямую — движок держит свой in-memory кэш UserState (self._users)
        и не перечитывает БД для уже закэшированных пользователей; прямая
        запись в store прошла бы мимо этого кэша и не изменила бы
        следующий вердикт активного чаттера."""
        if self.account_age_client is None:
            return
        assert self.engine is not None
        while True:
            try:
                if self._pending_account_age:
                    batch = list(self._pending_account_age)[:ACCOUNT_AGE_BATCH_SIZE]
                    users = await self.account_age_client.get_users(user_ids=batch)
                    for u in users:
                        await self.engine.update_account_age(u.id, u.created_at)
                        self._pending_account_age.discard(u.id)
                    # user_id, которых Helix не вернул (аккаунт удалён/забанен
                    # на стороне Twitch) — не повторять запрос бесконечно.
                    for user_id in batch:
                        self._pending_account_age.discard(user_id)
            except Exception:
                log.exception("Не удалось резолвить возраст аккаунта через Helix")
            await asyncio.sleep(ACCOUNT_AGE_POLL_SECONDS)

    async def _poll_action_queue(self) -> None:
        """Было main.py::_poll_action_queue. Исполняет задания, которые
        панель кладёт в mod_action_queue (BAN ALL/TIMEOUT ALL). Пересоздаёт
        ActionExecutor на каждый цикл со свежим access_token — тот сам
        решает, нужно ли реально идти в Twitch за обновлением."""
        if self.mod_token_manager is None or self.helix_client is None:
            return
        while True:
            try:
                access_token = await self.mod_token_manager.get_valid_access_token()
                state = self.mod_token_manager.state
                executor = ActionExecutor(
                    self.helix_client,
                    self.store,
                    broadcaster_id=state.broadcaster_id,
                    moderator_id=state.bot_user_id,
                    user_token=access_token,
                )
                processed = await process_pending(executor, self.store)
                if processed:
                    log.info("Обработано заданий из очереди модерации: %d", processed)
            except ModTokenError:
                log.exception(
                    "Токен модератора недействителен — получите новый в панели "
                    "(Settings -> Twitch: получить токен бота)"
                )
            except Exception:
                log.exception("Сбой обработки очереди действий модерации")
            await asyncio.sleep(ACTION_QUEUE_POLL_SECONDS)


async def _resolve_channel(broadcaster_id: str) -> str:
    """Читает login канала из своего registry.db по broadcaster_id — источник
    правды для конфигурации канала теперь Registry, не .env.<profile>."""
    registry = RegistryStore(str(paths.REGISTRY_DB))
    await registry.connect()
    try:
        record = await registry.get_channel(broadcaster_id)
    finally:
        await registry.close()
    if record is None:
        raise SystemExit(
            f"broadcaster_id={broadcaster_id!r} не найден в registry.db — "
            f"канал должен быть зарегистрирован (см. scripts/import_registry.py "
            f"или POST /api/channels в twitch-bots) до запуска consumer'а"
        )
    return record.login


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Использование: python -m cigilbot.consumer <broadcaster_id>")
    broadcaster_id = sys.argv[1]

    # Единственный .env монорепо — PANEL_TWITCH_CLIENT_ID/SECRET
    # (mod_token_manager), TWITCH_MOD_* и BOT_PROJECT_ROOT. Конфигурация
    # канала (login) не в .env, а в Registry.
    #
    # Тот же файл, в который panel/auth.py::_write_env_values кладёт
    # TWITCH_MOD_* после входа под аккаунтом бота. Если эти два пути
    # разойдутся, панель отрапортует о полученном токене, а executor
    # прочитает пустоту и все баны будут падать с 401.
    _load_env_file(paths.REPO_ROOT / ".env")

    paths.ensure_dirs()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    channel = asyncio.run(_resolve_channel(broadcaster_id))

    # bot.db живёт у twitch-bots (сосед по apps/), не здесь — BOT_PROJECT_ROOT
    # указывает на него явно, а не выводится угадыванием относительного
    # пути, чтобы расположение twitch-bots можно было переопределить без
    # правки кода (напр. другая машина, другой диск). Один общий bot.db на
    # все каналы бота (не bot.<instance>.db) — один бот-аккаунт обслуживает
    # все каналы сразу, инстансов больше нет.
    bot_project_root = Path(os.environ.get("BOT_PROJECT_ROOT", str(ROOT.parent / "twitch-bots")))
    bot_db_path = paths.bot_db(bot_project_root)

    mod_db_path = paths.mod_db(broadcaster_id)

    consumer = ModerationConsumer(
        broadcaster_id=broadcaster_id,
        bot_db_path=bot_db_path,
        mod_db_path=mod_db_path,
        channel=channel,
    )
    asyncio.run(consumer.start())


if __name__ == "__main__":
    main()
