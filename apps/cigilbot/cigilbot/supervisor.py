"""Фоновый луп, поддерживающий consumer-процессы в соответствии с
desired_state в registry.db.

Запускается как asyncio-таск ВНУТРИ процесса панели (apps/panel/panel/server.py
lifespan), не отдельным OS-процессом — компромисс, принятый явно: supervisor
не делает тяжёлой работы сам (только subprocess.Popen/taskkill + чтение
registry.db), реальная нагрузка всегда в отдельном процессе consumer.py.
Цена: если панель (порт 8766) падает, restart-on-crash для упавших
consumer'ов тоже останавливается до перезапуска панели — тот же класс
риска, что уже есть у twitch-bots сегодня (без панели никто не следит за
её ботом).

Тик раз в SUPERVISOR_INTERVAL_SECONDS: читает все активные каналы,
сверяет desired_state с реальной живостью процесса, запускает/
останавливает по расхождению. Restart-loop защита — если канал падает
слишком часто, supervisor перестаёт пытаться и требует ручного вмешательства
(reset_crash) — иначе постоянно падающий процесс (например, из-за неверного
токена) рестартовался бы бесконечно, засоряя логи и дёргая Twitch API.
"""

from __future__ import annotations

import asyncio
import logging
import time

from cigilbot.process_control import is_running, start_consumer, stop_consumer
from cigilbot.registry_store import ChannelRecord, RegistryStore

log = logging.getLogger("cigilbot.supervisor")

SUPERVISOR_INTERVAL_SECONDS = 10.0
RESTART_LIMIT = 5
RESTART_WINDOW_SECONDS = 600.0  # 10 минут


async def _tick(registry: RegistryStore) -> None:
    channels = await registry.list_channels(status="active")
    for channel in channels:
        await _reconcile_one(registry, channel)


async def _reconcile_one(registry: RegistryStore, channel: ChannelRecord) -> None:
    alive = is_running(channel.broadcaster_id)

    if channel.process_status == "crashed":
        # Restart-loop сработал ранее — ждём ручного reset_crash (см.
        # panel/registry_api.py), supervisor не трогает канал сам.
        return

    if channel.desired_state == "running" and not alive:
        # Последний рестарт был давно (за пределами окна) — считаем прошлые
        # падения устаревшими и даём каналу свежий кредит попыток. Без этого
        # канал, падающий раз в день на протяжении недели, был бы ошибочно
        # помечен "crashed", хотя падения не были частыми.
        if channel.last_heartbeat_at is not None and time.time() - channel.last_heartbeat_at > RESTART_WINDOW_SECONDS:
            await registry.reset_crash(channel.broadcaster_id)
            channel = await registry.get_channel(channel.broadcaster_id)  # type: ignore[assignment]

        if _restart_loop_triggered(channel):
            log.warning(
                "channel %s (%s): %d рестартов за %.0f сек — считаю сломанным, "
                "жду ручного reset_crash",
                channel.login, channel.broadcaster_id, channel.restart_count, RESTART_WINDOW_SECONDS,
            )
            await registry.update_process_state(channel.broadcaster_id, process_status="crashed")
            return

        log.info("channel %s (%s): запускаю consumer", channel.login, channel.broadcaster_id)
        pid = start_consumer(channel.broadcaster_id)
        await registry.update_process_state(
            channel.broadcaster_id, process_status="running", pid=pid, increment_restart_count=True
        )
        return

    if channel.desired_state == "stopped" and alive:
        log.info("channel %s (%s): останавливаю consumer", channel.login, channel.broadcaster_id)
        stop_consumer(channel.broadcaster_id)
        await registry.update_process_state(channel.broadcaster_id, process_status="stopped", pid=None)
        return

    # Состояние соответствует желаемому — просто обновляем heartbeat, чтобы
    # панель могла отличить "supervisor жив и следит" от "supervisor сам упал".
    if alive:
        await registry.update_process_state(
            channel.broadcaster_id, process_status="running", pid=channel.pid
        )


def _restart_loop_triggered(channel: ChannelRecord) -> bool:
    """RESTART_LIMIT рестартов внутри RESTART_WINDOW_SECONDS — считаем
    петлёй сбоев. Окно применяется в _reconcile_one (сброс restart_count,
    если последний рестарт был давно) до вызова этой функции — здесь
    остаётся только сравнение счётчика с порогом."""
    return channel.restart_count >= RESTART_LIMIT


async def supervisor_loop(registry_db_path: str) -> None:
    registry = RegistryStore(registry_db_path)
    await registry.connect()
    log.info("Cigilbot supervisor запущен (registry=%s)", registry_db_path)
    try:
        while True:
            try:
                await _tick(registry)
            except Exception:
                log.exception("Ошибка в тике supervisor'а — продолжаю следующий тик")
            await asyncio.sleep(SUPERVISOR_INTERVAL_SECONDS)
    finally:
        await registry.close()
