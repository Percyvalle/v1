"""Тесты детектора возраста аккаунта, первого сообщения и истории."""

from __future__ import annotations

import time

from cigilbot.detectors import account
from tests.conftest import EventFactory, make_context, make_user_state


class TestNewAccount:
    def test_flags_fresh_account(self, event_factory: EventFactory) -> None:
        event = event_factory()
        user = make_user_state(event, account_created_at=time.time() - 3600)  # час назад
        ctx = make_context(event, user=user)

        signals = account.detect(ctx)
        assert any(s.name == "new_account" for s in signals)

    def test_no_signal_for_old_account(self, event_factory: EventFactory) -> None:
        event = event_factory()
        user = make_user_state(event, account_created_at=time.time() - 365 * 86400)
        ctx = make_context(event, user=user)

        signals = account.detect(ctx)
        assert not any(s.name == "new_account" for s in signals)

    def test_unknown_account_age_no_signal(self, event_factory: EventFactory) -> None:
        # account_created_at ещё не пришёл от Helix — не значит "новый"
        event = event_factory()
        user = make_user_state(event, account_created_at=None)
        ctx = make_context(event, user=user)

        signals = account.detect(ctx)
        assert not any(s.name == "new_account" for s in signals)

    def test_value_higher_for_fresher_account(self, event_factory: EventFactory) -> None:
        event = event_factory()
        very_fresh = make_user_state(event, account_created_at=time.time() - 60)
        almost_week = make_user_state(event, account_created_at=time.time() - 6.5 * 86400)

        fresh_signal = next(
            s for s in account.detect(make_context(event, user=very_fresh)) if s.name == "new_account"
        )
        old_signal = next(
            s for s in account.detect(make_context(event, user=almost_week)) if s.name == "new_account"
        )
        assert fresh_signal.value > old_signal.value


class TestFirstMessage:
    def test_flags_first_message_tag(self, event_factory: EventFactory) -> None:
        event = event_factory(is_first_message=True)
        ctx = make_context(event)
        assert any(s.name == "first_message" for s in account.detect(ctx))

    def test_no_signal_without_tag(self, event_factory: EventFactory) -> None:
        event = event_factory(is_first_message=False)
        ctx = make_context(event)
        assert not any(s.name == "first_message" for s in account.detect(ctx))


class TestNoHistory:
    def test_flags_when_no_prior_messages(self, event_factory: EventFactory) -> None:
        event = event_factory()
        user = make_user_state(event, message_count=0)
        ctx = make_context(event, user=user)
        assert any(s.name == "no_history" for s in account.detect(ctx))

    def test_no_signal_with_established_history(self, event_factory: EventFactory) -> None:
        event = event_factory()
        user = make_user_state(event, message_count=50)
        ctx = make_context(event, user=user)
        assert not any(s.name == "no_history" for s in account.detect(ctx))


class TestCombination:
    def test_ordinary_new_viewer_gets_only_weak_identity_signals(
        self, event_factory: EventFactory
    ) -> None:
        # Обычный новый зритель — first_message + no_history, но НЕ
        # new_account (возраст аккаунта неизвестен на момент оценки).
        # Ни один из этих сигналов не должен сам по себе доводить до высокого риска.
        event = event_factory(is_first_message=True)
        user = make_user_state(event, message_count=0, account_created_at=None)
        ctx = make_context(event, user=user)

        signals = account.detect(ctx)
        total = sum(s.score for s in signals)
        assert total < ctx.config.risk.observe
