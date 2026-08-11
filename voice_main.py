import asyncio
import logging

import paths
from bot.audio_source import build_source
from bot.config import load_config
from bot.voice import VoiceListener
from bot.voice_queue import VoiceQueue

cfg = load_config()

# До basicConfig — см. тот же комментарий в main.py.
paths.ensure_dirs()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(cfg.log_path("voice"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("voicebot")

queue = VoiceQueue(cfg.queue_path)


async def on_transcript(text: str) -> None:
    queue.push(text)


async def main() -> None:
    source = build_source(cfg.voice_source, cfg.voice_stream_channel)
    listener = VoiceListener(
        on_transcript=on_transcript,
        source=source,
        silence_threshold=cfg.voice_silence_threshold,
    )
    await listener.start()
    log.info("Голосовой процесс запущен, источник: %s", source.name)
    await asyncio.Event().wait()  # работает вечно


if __name__ == "__main__":
    asyncio.run(main())
