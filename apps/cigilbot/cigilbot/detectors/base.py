"""Протокол детектора и контекст, который ему передаётся.

Ключевое архитектурное ограничение: DetectionContext не содержит ничего,
умеющего сеть или диск. Детектор физически не может отправить запрос в
Twitch — у него просто нет такой зависимости в сигнатуре. Действие
принимает решение дальше по цепочке (policy.py), детектор только наблюдает.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cigilbot.config import ChannelProfile, ModerationConfig
from cigilbot.normalize import MessageFingerprint
from cigilbot.types import ChannelContext, ChatEvent, Signal, UserState
from cigilbot.window import SlidingWindow


@dataclass(frozen=True, slots=True)
class DetectionContext:
    """Всё, что нужно детектору для оценки одного сообщения."""

    event: ChatEvent
    fingerprint: MessageFingerprint
    user: UserState
    window: SlidingWindow
    config: ModerationConfig
    channel_profile: ChannelProfile
    channel_context: ChannelContext


class Detector(Protocol):
    """Контракт детектора: чистая функция события в список сигналов.

    Расширяемость намеренно устроена так: новый детектор — это новый файл
    с функцией такой сигнатуры плюс строка в реестре (detectors/__init__.py).
    Ни scoring.py, ни policy.py, ни панель при этом не меняются — они
    работают с абстракцией Signal, а не со списком известных проверок.
    """

    name: str

    def detect(self, ctx: DetectionContext) -> list[Signal]: ...
