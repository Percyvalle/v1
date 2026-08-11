import asyncio
import difflib
import logging
import random
import re
import sys
import time
from pathlib import Path

from twitchio.ext import commands

from bot import paths
from bot.brain import Brain
from bot.config import load_config
from bot.database import Database
from bot.registry import ChannelRegistry
from bot.voice_queue import VoiceQueue

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

cfg = load_config()

# До basicConfig: FileHandler ниже открывает файл сразу и падает, если
# каталога нет. На чистом клоне var/ не существует вовсе — он целиком в
# .gitignore, там нечему быть в репозитории.
paths.ensure_dirs()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(cfg.log_path("bot"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("twitchbot")

MIN_VOICE_WORDS = 3  # фразы короче этого игнорируются (шум, обрывки, "угу")
# Насколько похожим должно быть слово, чтобы считаться обращением к боту.
# Распознавание речи даёт падежи и искажения ("Марата", "Шинры", "Синра"),
# поэтому сравниваем нестрого.
VOICE_TRIGGER_SIMILARITY = 0.85
# Сколько последних распознанных фраз склеивать в одну реплику боту
VOICE_BATCH_MERGE = 3
# После обращения без имени, на которое бот решил ОТВЕТИТЬ, столько секунд
# не даём ему даже спрашивать модель "реагировать?" на новые обрывки речи.
# Без этого стример, говорящий без пауз, получает ответ почти на каждую
# фразу подряд: модель периодически решает "да" на соседние куски одного
# монолога, а внешнего ограничения этому решению раньше не было.
# Настраивается через .env (VOICE_FREE_REPLY_COOLDOWN), правится из панели.
VOICE_FREE_REPLY_COOLDOWN = cfg.voice_free_reply_cooldown

# Имитация "печати" перед отправкой — задержка растёт с длиной ответа, плюс
# случайный разброс, чтобы не быть одинаковой каждый раз. Подобрано так,
# чтобы типичный короткий ответ приходил через 3-8 сек — мгновенный ответ
# на любое сообщение выдавал бота с головой.
TYPING_BASE_DELAY = 2.5
TYPING_SECONDS_PER_CHAR = 0.05
TYPING_MAX_DELAY = 8.0

# Если несколько зрителей почти одновременно пишут боту что-то похожее по
# смыслу ("шинра ты тут" / "шинра ты живой" / "шинра ау"), не идём в LLM за
# отдельным ответом на каждое — реагируем на первое, остальные молча
# игнорируем на это время, как будто бот "не успевает всех прочитать".
DUPLICATE_WINDOW_SECONDS = 25
DUPLICATE_SIMILARITY = 0.6

# Минимальный интервал между ЛЮБЫМИ двумя сообщениями бота в чат, кем бы они
# ни были вызваны (голос или несколько зрителей, написавших триггер разом).
# Если просто ждать перед отправкой каждого ответа по отдельности — это не
# спасает от залпа: N параллельных обращений в чат порождают N параллельных
# запросов к DeepSeek, и все N ответов всё равно прилетают почти одновременно,
# каждый со своей "печатной" задержкой, но без учёта соседей. Здесь вместо
# этого все сообщения идут через одну очередь с одним "ртом" — отправляются
# строго по одному, с паузой между ними.
MIN_MESSAGE_INTERVAL = 6.0


_IRC_TAG_ESCAPES = {r"\s": " ", r"\:": ";", r"\\": "\\", r"\r": "\r", r"\n": "\n"}
_IRC_TAG_ESCAPE_RE = re.compile("|".join(re.escape(k) for k in _IRC_TAG_ESCAPES))


def _unescape_irc_tag(value: str) -> str:
    return _IRC_TAG_ESCAPE_RE.sub(lambda m: _IRC_TAG_ESCAPES[m.group()], value)


def _get_reply_parent(message) -> tuple[str, str] | None:
    """Если сообщение — reply на другое, вернуть (автор, текст) родителя.

    Twitch присылает это как IRC-теги reply-parent-msg-body/-user-login —
    twitchio их не парсит отдельно, но кладёт весь сырой набор тегов в
    message.tags, так что можно достать напрямую. Значения IRC-экранированы
    (пробел как \\s и т.п.), поэтому распаковываем вручную.
    """
    tags = message.tags or {}
    body = tags.get("reply-parent-msg-body")
    author = tags.get("reply-parent-user-login") or tags.get("reply-parent-display-name")
    if not body or not author:
        return None
    return _unescape_irc_tag(author), _unescape_irc_tag(body)


def _normalize_for_dedup(text: str) -> str:
    # Триггер-слово ("шинра", "марат") выкидываем — иначе почти любые два
    # обращения к боту совпадут просто из-за общего имени в тексте.
    words = re.findall(r"\w+", text.lower())
    filtered = [w for w in words if not any(t in w for t in cfg.triggers + cfg.voice_triggers)]
    return " ".join(filtered)


class DuplicateFilter:
    """Отбрасывает сообщения, похожие на недавно обработанные.

    Нужен, чтобы несколько зрителей, почти одновременно написавших боту
    что-то похожее по смыслу, не порождали отдельный запрос к LLM на
    каждого — реагируем на первого, остальных как будто "не успели прочитать".
    """

    def __init__(self):
        self._recent: list[tuple[float, str]] = []

    def is_duplicate(self, text: str) -> bool:
        now = time.monotonic()
        self._recent = [
            (ts, norm) for ts, norm in self._recent if now - ts < DUPLICATE_WINDOW_SECONDS
        ]

        normalized = _normalize_for_dedup(text)

        for _, norm in self._recent:
            # Пустая нормализация — голое имя бота без ничего вокруг
            # ("шинра", "марат марат") — само по себе уже дубль такого же.
            if not normalized and not norm:
                return True
            if normalized and norm and self._similar(normalized, norm):
                return True

        self._recent.append((now, normalized))
        return False

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        # Короткие фразы (1-2 слова) отличаются на пару букв даже при том же
        # смысле ("ты тут" / "ты жив"), поэтому для них порог ниже.
        threshold = DUPLICATE_SIMILARITY if len(a) > 12 and len(b) > 12 else 0.45
        return ratio >= threshold


class MessageQueue:
    """Единая точка отправки в чат: сериализует все сообщения бота.

    Не полагается на то, что вызовы придут по одному — event_message
    от twitchio обрабатывает разных зрителей параллельными корутинами,
    поэтому очередь и лок нужны именно здесь, а не "подождать перед
    отправкой" в каждом обработчике по отдельности.

    Один бот слушает несколько каналов сразу (initial_channels списком) —
    очередь общая на все каналы (MIN_MESSAGE_INTERVAL держит бота от
    спама в целом, не по каждому каналу отдельно), но каждое сообщение
    несёт свой channel_name, чтобы уйти в правильный чат.
    """

    def __init__(self, channel_getter):
        self._get_channel = channel_getter
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._last_sent_at = 0.0

    def start(self) -> None:
        asyncio.create_task(self._run())

    async def send(self, channel_name: str, text: str) -> None:
        await self._queue.put((channel_name, text))

    async def _run(self) -> None:
        while True:
            channel_name, text = await self._queue.get()

            wait = MIN_MESSAGE_INTERVAL - (time.monotonic() - self._last_sent_at)
            if wait > 0:
                await asyncio.sleep(wait)

            await asyncio.sleep(min(TYPING_BASE_DELAY + len(text) * TYPING_SECONDS_PER_CHAR, TYPING_MAX_DELAY) * random.uniform(0.8, 1.2))

            channel = self._get_channel(channel_name)
            if channel is not None:
                await channel.send(text)
                await db.log_message(cfg.bot_nick, text)
            self._last_sent_at = time.monotonic()


db = Database(cfg.db_path)
# None — чисто модераторский профиль (см. CIGILBOT): бот наблюдает и банит
# через Cigilbot (отдельный проект/процесс), но ничего не пишет в чат сам
# по себе. Личность бота (streamer_context, характер) — одна на ВСЕ каналы
# сразу, не per-channel: один бот-аккаунт обслуживает все каналы, отдельная
# личность на каждый — не задача этого этапа (см. docs/master-plan.html,
# направление 00, обсуждение LLM-личности при multi-channel).
brain = (
    Brain(cfg.deepseek_api_key, cfg.personality, cfg.channel, cfg.usage_path, cfg.streamer_context)
    if cfg.deepseek_api_key
    else None
)
voice_queue = VoiceQueue(cfg.queue_path)

# Модерация (движок, панель, исполнение BAN/TIMEOUT) — отдельный процесс
# Cigilbot (../cigilbot), не часть main.py. Этот бот только пишет каждое
# сообщение чата в очередь mod_inbox (в своей же bot.db, см.
# bot/database.py::SCHEMA) и продолжает читать чат дальше, не дожидаясь
# ответа — Cigilbot читает эту очередь в фоне из своего процесса
# (cigilbot/consumer.py), main.py никогда не блокируется и не падает,
# если Cigilbot медленный или не запущен.


async def _load_initial_channels() -> list[str]:
    """Список каналов из Channel Registry (registry.db) — источник правды
    для того, куда подключается бот. Один Twitch-бот-аккаунт слушает ВСЕ
    активные каналы сразу через initial_channels (twitchio поддерживает это
    нативно), а не по одному процессу на канал, как раньше через
    .env.<profile> (см. docs/master-plan.html, направление 00).

    cfg.channel остаётся как fallback — если Registry пуст (например при
    первом запуске до того, как через панель добавили хоть один канал),
    бот всё равно подключается к каналу из .env, чтобы не остаться совсем
    без подключения."""
    registry = ChannelRegistry(str(paths.VAR / "registry.db"))
    await registry.connect()
    try:
        channels = await registry.list_channels(status="active")
    finally:
        await registry.close()
    logins = [c.login for c in channels]
    return logins or ([cfg.channel] if cfg.channel else [])


class ChatBot(commands.Bot):
    def __init__(self, initial_channels: list[str]):
        self.channels_list = initial_channels
        super().__init__(
            token=cfg.bot_token,
            nick=cfg.bot_nick,
            prefix="!",
            initial_channels=initial_channels,
        )

    async def event_command_error(self, context, error: Exception) -> None:
        """Зрители часто пишут команды других ботов (!followage, !uptime
        и т.п.), которых у нас нет — twitchio по умолчанию печатает это в
        stderr как traceback на КАЖДЫЙ такой случай (см. commands.Bot
        ::event_command_error), хотя это ожидаемое поведение, не ошибка.
        Тихо глотаем только CommandNotFound; любая другая ошибка внутри
        реальной команды (!remember, !botinfo) по-прежнему логируется —
        это баг в нашем коде, который нельзя прятать молча."""
        if isinstance(error, commands.CommandNotFound):
            return
        log.exception("Ошибка при выполнении команды чата", exc_info=error)

    async def event_ready(self):
        await db.connect()
        if cfg.moderation_enabled:
            log.info(
                "Модерация включена: сообщения чата пишутся в mod_inbox для Cigilbot "
                "(запустите .venv\\Scripts\\python -m cigilbot.consumer <broadcaster_id> "
                "в соседнем проекте Cigilbot на каждый канал, если ещё не запущено)"
            )
        log.info("Бот подключился как %s к каналам: %s", self.nick, ", ".join(self.channels_list))

        self._last_free_reply_at = 0.0
        self._outbox = MessageQueue(self.get_channel)
        self._outbox.start()
        self._dedup = DuplicateFilter()

        if cfg.voice_enabled:
            asyncio.create_task(self._poll_voice_queue())

    async def _poll_voice_queue(self) -> None:
        log.info("Слежу за голосовыми сообщениями (файл %s)", voice_queue.path)
        while True:
            lines = voice_queue.pop_all()
            if lines:
                await self._handle_voice_batch(lines)
            await asyncio.sleep(1)

    async def _handle_voice_batch(self, lines: list[str]) -> None:
        """Склеиваем последние фразы в одну реплику, остальное выкидываем.

        Распознавание режет речь по паузам, поэтому обращение легко
        разъезжается на куски: "Марат" и "что думаешь?" приходят отдельно.
        Склейка сохраняет и имя, и сам вопрос. Всё, что старше этих фраз,
        выбрасываем — отвечать надо на живой разговор, а не догонять его.
        """
        recent = lines[-VOICE_BATCH_MERGE:]
        dropped = len(lines) - len(recent)
        if dropped:
            log.info("Пропускаю %d устаревших фраз", dropped)

        await self._on_voice_transcript(" ".join(recent))

    async def _on_voice_transcript(self, text: str) -> None:
        if brain is None:
            return
        log.info("Голосовое сообщение: %s", text)

        addressed_directly = self._is_voice_addressed_to_bot(text)
        if addressed_directly:
            log.info("Голос: прямое обращение по имени, отвечаю сразу")

        # Обращение по имени — всегда мгновенный ответ, без исключений.
        if not addressed_directly:
            # Без имени решение "стоит ли встревать" в первую очередь
            # принимает сама модель (см. brain.maybe_reply_to_voice), но её
            # спрашиваем не чаще, чем раз в VOICE_FREE_REPLY_COOLDOWN секунд
            # после последнего такого ответа — иначе на безостановочный
            # монолог модель периодически отвечает "да" на соседние обрывки,
            # и в чат летит пачка сообщений подряд.
            if cfg.voice_require_trigger:
                return
            if len(text.split()) < MIN_VOICE_WORDS:
                return
            if time.monotonic() - self._last_free_reply_at < VOICE_FREE_REPLY_COOLDOWN:
                return

        await db.touch_viewer(cfg.streamer_name)
        await db.log_message(cfg.streamer_name, text)

        try:
            context = await db.get_recent_context()
            viewer = await db.get_viewer(cfg.streamer_name)
            note = viewer["note"] if viewer else None

            if addressed_directly:
                reply = await brain.reply(cfg.streamer_name, text, context, note)
            else:
                reply = await brain.maybe_reply_to_voice(cfg.streamer_name, text, context, note)
                if reply is None:
                    return
                self._last_free_reply_at = time.monotonic()

            # Голос физически привязан к одному стриму (микрофон/аудиодорожка
            # этого компьютера) — cfg.voice_stream_channel, не список
            # каналов бота, даже при multi-channel режиме.
            await self._outbox.send(cfg.voice_stream_channel, reply)
        except Exception:
            log.exception("Не удалось сгенерировать ответ на голос")

    async def event_raw_usernotice(self, channel, tags: dict) -> None:
        """FALSE-BAN-001 аудита: Twitch присылает USERNOTICE с msg-id=raid,
        когда канал получает реальный рейд — twitchio отдаёт это событие
        бесплатно, без EventSub-подписки. Раньше это напрямую звало
        moderation_engine.mark_raid_started() в этом же процессе; теперь
        модерация — процесс Cigilbot, поэтому кладём событие в ту же
        очередь mod_inbox, что и обычные сообщения чата (см.
        cigilbot/consumer.py::_handle_inbox_item, kind="raid_started")."""
        if not cfg.moderation_enabled or tags.get("msg-id") != "raid":
            return
        raider = tags.get("msg-param-displayName") or tags.get("login", "?")
        viewer_count = tags.get("msg-param-viewerCount", "?")
        channel_name = channel.name if channel is not None else ""
        log.info("Рейд от %s (%s зрителей) на канал %s — передаю Cigilbot", raider, viewer_count, channel_name)
        try:
            # channel обязателен: один бот слушает несколько каналов сразу
            # (initial_channels списком), без него Cigilbot не смог бы
            # определить, чей consumer должен снизить чувствительность на
            # время рейда (см. cigilbot/inbox.py::ChatInbox.get_pending).
            await db.enqueue_chat_event({"kind": "raid_started", "channel": channel_name})
        except Exception:
            log.exception("Не удалось поставить raid_started в очередь модерации")

    async def event_message(self, message):
        if message.echo:
            return

        username = message.author.name
        content = message.content

        await db.touch_viewer(username)
        await db.log_message(username, content)

        if cfg.moderation_enabled:
            await self._enqueue_moderation(message, username, content)

        await self.handle_commands(message)

        if self._is_addressed_to_bot(content):
            if self._dedup.is_duplicate(content):
                log.info("Похоже на недавнее обращение, игнорирую: %s", content)
                return
            await self._respond(message, username, content)

    async def _enqueue_moderation(self, message, username: str, content: str) -> None:
        """Кладёт сообщение чата в mod_inbox для Cigilbot вместо прямого
        in-process вызова ModerationEngine.observe() (движок теперь живёт
        в отдельном процессе — см. ../cigilbot/cigilbot/consumer.py).

        Сбой здесь никогда не должен ронять обработку сообщения ботом —
        модерация лишь наблюдает, а не является частью основного пути.
        Не ждём и не блокируемся на Cigilbot: enqueue — это просто INSERT
        в свою же локальную БД, дальше бот сразу читает следующее сообщение.
        """
        author = message.author
        user_id = author.id if author else None
        if not user_id:
            # Без user-id (тег Twitch, приходит вместе с остальными тегами)
            # оценивать нечего — такое означает проблему с CAP REQ tags,
            # а не реальное отсутствие данных о конкретном сообщении.
            return

        tags = message.tags or {}
        try:
            await db.enqueue_chat_event(
                {
                    "kind": "chat_message",
                    "user_id": user_id,
                    "login": username,
                    "text": content,
                    "timestamp": time.time(),
                    # message.channel.name, не cfg.channel — один бот слушает
                    # несколько каналов сразу, событие должно нести КОНКРЕТНЫЙ
                    # канал, из которого пришло это сообщение (см.
                    # cigilbot/inbox.py::ChatInbox.get_pending, фильтрует по
                    # этому полю, чтобы consumer видел только свой канал).
                    "channel": message.channel.name if message.channel else "",
                    "display_name": author.display_name or username,
                    "message_id": message.id or "",
                    "is_first_message": message.first,
                    "is_returning_chatter": tags.get("returning-chatter") == "1",
                    "is_subscriber": author.is_subscriber,
                    "is_moderator": author.is_mod,
                    "is_vip": author.is_vip,
                    "is_broadcaster": author.is_broadcaster,
                    "badges": list(author.badges.keys()),
                }
            )
        except Exception:
            log.exception("Не удалось поставить сообщение в очередь модерации")

    def _is_addressed_to_bot(self, content: str) -> bool:
        if content.startswith("!"):
            return False
        lowered = content.lower()
        # @ник — Twitch подставляет его при клике "ответить" или автодополнении,
        # это такое же явное обращение к боту, как и слово-триггер.
        if f"@{cfg.bot_nick.lower()}" in lowered:
            return True
        return any(
            re.search(rf"(^|\W){re.escape(t)}(\W|$)", lowered) for t in cfg.triggers
        )

    def _is_voice_addressed_to_bot(self, text: str) -> bool:
        """То же, но для распознанной речи — нестрого.

        Whisper коверкает имя: "Марата", "Шинры", "Синра", "Шершень". Точное
        совпадение слова тут почти никогда не срабатывает, поэтому сравниваем
        каждое слово фразы с триггерами по похожести и по началу слова
        (падежные окончания: "Марату", "Шинрой").
        """
        for word in re.findall(r"\w+", text.lower()):
            for trigger in cfg.voice_triggers:
                # trigger in word ловит слипшееся с соседним словом имя:
                # "Башинро", "эйшинра" — распознавание часто их не разделяет
                if trigger in word:
                    return True
                ratio = difflib.SequenceMatcher(None, word, trigger).ratio()
                if ratio >= VOICE_TRIGGER_SIMILARITY:
                    return True
        return False

    async def _respond(self, message, username: str, content: str):
        if brain is None:
            return
        try:
            context = await db.get_recent_context()
            viewer = await db.get_viewer(username)
            note = viewer["note"] if viewer else None
            reply_parent = _get_reply_parent(message)

            reply = await brain.maybe_reply_in_chat(
                username, content, context, note, reply_parent
            )
            if reply is None:
                return
            channel_name = message.channel.name if message.channel else cfg.channel
            await self._outbox.send(channel_name, f"@{username} {reply}")
        except Exception:
            log.exception("Не удалось сгенерировать ответ")

    @commands.command(name="remember")
    async def remember(self, ctx: commands.Context, *, note: str = ""):
        """Модератор/стример может добавить заметку о зрителе: !remember ник текст"""
        # ctx.channel.name — канал, где написали команду, не cfg.channel
        # (единственный канал раньше) — при multi-channel стример каждого
        # отдельного канала должен уметь пользоваться командой в своём чате.
        is_broadcaster = ctx.channel is not None and ctx.author.name.lower() == ctx.channel.name.lower()
        if not (ctx.author.is_mod or is_broadcaster):
            return
        parts = note.split(maxsplit=1)
        if len(parts) < 2:
            await ctx.send("Использование: !remember ник текст заметки")
            return
        target, text = parts
        await db.set_note(target.lower(), text)
        await ctx.send(f"Запомнил про {target}: {text}")

    @commands.command(name="botinfo")
    async def botinfo(self, ctx: commands.Context):
        names = " / ".join(cfg.triggers)
        await ctx.send(f"Я ИИ-бот этого канала. Позовите меня: {names}")


if __name__ == "__main__":
    # ChatBot.__init__ (через twitchio.Client.__init__) вызывает
    # asyncio.get_event_loop() синхронно — в Python 3.12 это падает, если
    # нет текущего loop в потоке (asyncio.run() в _load_initial_channels()
    # ниже создаёт свой loop и закрывает его по выходу). Явно создаём и
    # устанавливаем loop перед созданием бота, тот же loop потом использует
    # bot.run() изнутри twitchio.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    channels = loop.run_until_complete(_load_initial_channels())
    if not channels:
        raise SystemExit(
            "Нет ни одного активного канала (Channel Registry пуст и TWITCH_CHANNEL "
            "в .env не задан) — добавьте канал через панель перед запуском"
        )
    log.info("Список каналов для подключения: %s", ", ".join(channels))
    bot = ChatBot(channels)
    bot.run()
