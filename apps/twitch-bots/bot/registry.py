"""Channel Registry — control-plane БД со списком каналов бота (registry.db).

Отдельный файл от bot.<instance>.db (bot/database.py) — этот файл не
per-инстанс, а общий: источник правды по составу каналов ("какие каналы
вообще существует, слушает ли их сейчас процесс бота"). twitch-bots —
источник правды для Cigilbot, который ведёт свою НЕЗАВИСИМУЮ копию Registry
(Cigilbot/cigilbot/registry_store.py) и синхронизируется через
POST /api/registry/channels (см. panel/server.py и Cigilbot/panel/registry_api.py).
Подробности архитектуры — docs/master-plan.html, направление 00.

В отличие от bot/database.py (схема без версионирования, executescript при
каждом connect) здесь схема версионируется через PRAGMA user_version —
Registry будет расти новыми полями по мере Phase 1/2, версионирование
дешевле сделать сразу, чем добавлять задним числом.

channels — один на канал, ключ broadcaster_id (стабильный Twitch ID, не
меняется при переименовании канала; login — только кэш для отображения).
Управление процессом main.py (start/stop) живёт в
Cigilbot/cigilbot/bot_process_control.py — оператор перезапускает бота
вручную через кнопку на панели 8766 после изменения состава каналов
(twitchio не поддерживает добавление канала в уже подключённый бот без
реконнекта). Автоматический restart здесь намеренно не реализован: один
main.py-процесс слушает ВСЕ каналы сразу, поэтому авто-restart оборвал бы
соединение и на каналах, где прямо сейчас идёт стрим — риск потерять
модерацию в разгар атаки. Раньше здесь была таблица bot_process с полем
restart_pending под будущий авто-supervisor этого сценария — убрана как
мёртвый код (ничего её не читало), см. историю Phase 1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiosqlite

_MIGRATION_001_CHANNELS = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcaster_id TEXT NOT NULL UNIQUE,
    login TEXT NOT NULL,
    display_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    voice_enabled INTEGER NOT NULL DEFAULT 0,
    moderation_enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status);
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _MIGRATION_001_CHANNELS),
)


async def _current_version(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def migrate(conn: aiosqlite.Connection) -> int:
    current = await _current_version(conn)
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        await conn.executescript(sql)
        await conn.execute(f"PRAGMA user_version = {version}")
        current = version
    await conn.commit()
    return current


@dataclass(frozen=True, slots=True)
class ChannelRecord:
    id: int
    broadcaster_id: str
    login: str
    display_name: str | None
    status: str
    voice_enabled: bool
    moderation_enabled: bool
    created_at: float
    updated_at: float


def _row_to_channel(row: aiosqlite.Row) -> ChannelRecord:
    return ChannelRecord(
        id=row["id"],
        broadcaster_id=row["broadcaster_id"],
        login=row["login"],
        display_name=row["display_name"],
        status=row["status"],
        voice_enabled=bool(row["voice_enabled"]),
        moderation_enabled=bool(row["moderation_enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ChannelRegistry:
    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # См. Cigilbot/cigilbot/registry_store.py::connect — тот же явный
        # busy_timeout вместо мгновенного "database is locked" при
        # конкурентной записи.
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await migrate(self._conn)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ChannelRegistry.connect() ещё не вызван")
        return self._conn

    async def upsert_channel(
        self,
        *,
        broadcaster_id: str,
        login: str,
        display_name: str | None = None,
        voice_enabled: bool = False,
        moderation_enabled: bool = True,
    ) -> ChannelRecord:
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO channels
                (broadcaster_id, login, display_name, voice_enabled, moderation_enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(broadcaster_id) DO UPDATE SET
                login = excluded.login,
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (broadcaster_id, login, display_name, int(voice_enabled), int(moderation_enabled), now, now),
        )
        await self._db.commit()
        return await self.get_channel(broadcaster_id)  # type: ignore[return-value]

    async def get_channel(self, broadcaster_id: str) -> ChannelRecord | None:
        cursor = await self._db.execute(
            "SELECT * FROM channels WHERE broadcaster_id = ?", (broadcaster_id,)
        )
        row = await cursor.fetchone()
        return _row_to_channel(row) if row else None

    async def list_channels(self, *, status: str | None = "active") -> list[ChannelRecord]:
        if status is None:
            cursor = await self._db.execute("SELECT * FROM channels ORDER BY login")
        else:
            cursor = await self._db.execute(
                "SELECT * FROM channels WHERE status = ? ORDER BY login", (status,)
            )
        rows = await cursor.fetchall()
        return [_row_to_channel(row) for row in rows]

