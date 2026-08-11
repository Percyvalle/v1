import json
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS viewers (
    username TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS recent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Исходящая очередь чата для Cigilbot (отдельный процесс/проект, своя БД
-- mod.<instance>.db). main.py пишет сюда каждое сообщение чата вместо
-- прямого in-process вызова ModerationEngine.observe(); Cigilbot открывает
-- своё соединение к ЭТОМУ файлу (bot.<instance>.db) только для этой одной
-- таблицы и поллит её в фоне. WAL-режим (включается ниже) — обязателен,
-- иначе параллельная запись/чтение из двух процессов будет блокироваться.
CREATE TABLE IF NOT EXISTS mod_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    event_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_mod_inbox_status ON mod_inbox(status);

-- panel_admins здесь больше не создаётся. ADMIN-оверрайды ролей панели
-- пережили полный круг: сначала общий список mod_panel_users на обе
-- панели, потом (когда панели разнесли по разным процессам) отдельный
-- panel_admins здесь и mod_panel_users в Cigilbot, теперь — снова один
-- список в mod_panel_users, потому что панель снова одна.
--
-- В уже существующих bot.db таблица остаётся лежать как есть: SQLite её
-- не удаляет, а удалять самим значило бы потерять данные у того, кто не
-- прогнал перенос. Разовый scripts/merge_panel_admins.py в apps/panel
-- переносит записи в mod_panel_users; после него таблица не читается
-- никем и её можно дропнуть вручную.
"""

# Сколько последних сообщений чата держим для контекста ответов бота
CONTEXT_WINDOW = 30


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        # WAL — обязателен для mod_inbox: Cigilbot открывает своё отдельное
        # соединение к этому же файлу параллельно (см. SCHEMA выше), и без
        # WAL конкурентная запись/чтение блокировались бы друг на друга.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def touch_viewer(self, username: str) -> None:
        now = time.time()
        await self._conn.execute(
            """
            INSERT INTO viewers (username, first_seen, last_seen, message_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET
                last_seen = excluded.last_seen,
                message_count = message_count + 1
            """,
            (username, now, now),
        )
        await self._conn.commit()

    async def get_viewer(self, username: str) -> aiosqlite.Row | None:
        self._conn.row_factory = aiosqlite.Row
        cursor = await self._conn.execute(
            "SELECT * FROM viewers WHERE username = ?", (username,)
        )
        return await cursor.fetchone()

    async def set_note(self, username: str, note: str) -> None:
        await self._conn.execute(
            "UPDATE viewers SET note = ? WHERE username = ?", (note, username)
        )
        await self._conn.commit()

    async def log_message(self, username: str, content: str) -> None:
        await self._conn.execute(
            "INSERT INTO recent_messages (username, content, created_at) VALUES (?, ?, ?)",
            (username, content, time.time()),
        )
        await self._conn.commit()

    async def get_recent_context(self, limit: int = CONTEXT_WINDOW) -> list[tuple[str, str]]:
        self._conn.row_factory = aiosqlite.Row
        cursor = await self._conn.execute(
            "SELECT username, content FROM recent_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [(row["username"], row["content"]) for row in reversed(rows)]

    # -- исходящая очередь для Cigilbot (см. SCHEMA::mod_inbox) -----------

    async def enqueue_chat_event(self, event: dict[str, Any]) -> None:
        """Кладёт сериализованный ChatEvent в очередь для Cigilbot.
        Не ждёт ответа и не блокирует event_message — Cigilbot читает эту
        таблицу из своего процесса, независимо от того, запущен он сейчас
        или нет (задания просто накопятся, пока Cigilbot не поднимется)."""
        await self._conn.execute(
            "INSERT INTO mod_inbox (created_at, event_json, status) VALUES (?, ?, 'pending')",
            (time.time(), json.dumps(event, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def prune_mod_inbox(self, *, older_than_seconds: float, keep_pending: bool = True) -> int:
        """Чистит обработанные записи mod_inbox, чтобы очередь не росла
        бесконечно при активном чате. keep_pending=True никогда не трогает
        status='pending' — даже если Cigilbot долго не забирал задания, они
        не потеряются, только 'done' старше порога удаляются."""
        cutoff = time.time() - older_than_seconds
        status_filter = "status = 'done'" if keep_pending else "status != 'pending'"
        cursor = await self._conn.execute(
            f"DELETE FROM mod_inbox WHERE {status_filter} AND created_at < ?",
            (cutoff,),
        )
        await self._conn.commit()
        return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0

    # ADMIN-оверрайды ролей панели читаются и пишутся через
    # ModerationStore (mod_panel_users) — get_panel_role/upsert_panel_admin/
    # list_panel_admins убраны отсюда вместе со слиянием панелей в один
    # процесс. Бот ролями панели не пользовался никогда: эти методы
    # существовали ради panel/server.py, которого больше нет.
