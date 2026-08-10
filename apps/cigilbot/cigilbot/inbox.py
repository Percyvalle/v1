"""Чтение входящей очереди чата (mod_inbox) из чужого файла БД.

mod_inbox живёт в bot.db — файле twitch-bots, не Cigilbot (см.
bot/database.py::SCHEMA в twitch-bots). main.py пишет туда каждое сообщение
чата вместо прямого in-process вызова ModerationEngine.observe(). Это
единственное место, где Cigilbot открывает соединение к чужому файлу —
всё остальное состояние модерации живёт в собственной mod.<broadcaster_id>.db
(см. store.py). WAL-режим на стороне twitch-bots (bot/database.py::connect)
делает параллельный доступ из двух процессов безопасным.

Один Twitch-бот-аккаунт обслуживает ВСЕ каналы сразу (main.py слушает их
через initial_channels списком, см. docs/master-plan.html,
направление 00) — значит mod_inbox теперь общая очередь для всех каналов,
а не одного. Cigilbot по-прежнему держит один consumer-процесс на канал
(движок стейтфул, смешивать состояние разных каналов не нужно), поэтому
ChatInbox фильтрует по каналу прямо в SQL через json_extract — без фильтра
каждый consumer видел бы (и мог бы пометить done) чужие сообщения."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite


@dataclass(frozen=True, slots=True)
class InboxItem:
    id: int
    event: dict[str, Any]


class ChatInbox:
    def __init__(self, bot_db_path: str, *, channel_login: str):
        self._path = bot_db_path
        self._channel_login = channel_login
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Не создаёт mod_inbox — таблица уже создана bot/database.py при
        # старте main.py. Если её ещё нет (main.py не запускался ни разу),
        # get_pending() ниже вернёт пустой список, а не упадёт: SQLite не
        # ошибается на SELECT из существующего файла без нужной таблицы,
        # ошибка будет явной (OperationalError) и это осознанный сигнал,
        # что main.py нужно запустить первым хотя бы раз.

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ChatInbox.connect() ещё не вызван")
        return self._conn

    async def get_pending(self, *, limit: int = 50) -> list[InboxItem]:
        """Только записи для СВОЕГО канала. raid_started тоже несёт channel
        (main.py::event_raw_usernotice) — без него рейд на канал A ошибочно
        снижал бы чувствительность и на канале B тоже. Фолбэк на IS NULL
        оставлен на случай будущих типов событий без канала в payload."""
        self._db.row_factory = aiosqlite.Row
        cursor = await self._db.execute(
            """
            SELECT id, event_json FROM mod_inbox
            WHERE status = 'pending'
              AND (json_extract(event_json, '$.channel') = ? OR json_extract(event_json, '$.channel') IS NULL)
            ORDER BY id LIMIT ?
            """,
            (self._channel_login, limit),
        )
        rows = await cursor.fetchall()
        return [InboxItem(id=row["id"], event=json.loads(row["event_json"])) for row in rows]

    async def mark_done(self, item_id: int) -> None:
        await self._db.execute(
            "UPDATE mod_inbox SET status = 'done' WHERE id = ?", (item_id,)
        )
        await self._db.commit()

    async def mark_done_many(self, item_ids: list[int]) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        await self._db.execute(
            f"UPDATE mod_inbox SET status = 'done' WHERE id IN ({placeholders})",
            item_ids,
        )
        await self._db.commit()
