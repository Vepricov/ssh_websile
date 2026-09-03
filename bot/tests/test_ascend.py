import asyncio

import pytest

from server_access_bot.app import deliver_ascend_reminders
from server_access_bot.domain import Application
from server_access_bot.storage import PendingRequestExistsError, Storage


def ascend_application() -> Application:
    return Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Example",
        server_username="alice",
        access_term="бессрочно",
        recommender="Andrey Example",
        activity="Ascend experiments",
        servers=("alibaba_ascend",),
    )


def test_ascend_request_reminds_admin_daily_until_resolved(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request(
        "request-1", ascend_application(), remind_at=100
    )

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    assert asyncio.run(
        deliver_ascend_reminders(
            bot, storage, frozenset({999}), now=99
        )
    ) == ()
    assert asyncio.run(
        deliver_ascend_reminders(
            bot, storage, frozenset({999}), now=100
        )
    ) == ("request-1",)
    assert "Доступ на Ascend уже выдали?" in bot.messages[-1][1]
    assert (
        bot.messages[-1][2]["reply_markup"].inline_keyboard[1][0].callback_data
        == "admin:ascend:grant:request-1"
    )

    assert asyncio.run(
        deliver_ascend_reminders(
            bot, storage, frozenset({999}), now=100 + 86399
        )
    ) == ()
    assert asyncio.run(
        deliver_ascend_reminders(
            bot, storage, frozenset({999}), now=100 + 86400
        )
    ) == ("request-1",)

    application = storage.claim_pending_request("request-1")
    assert application is not None
    storage.resolve_pending_request("request-1", "approved")
    assert asyncio.run(
        deliver_ascend_reminders(
            bot, storage, frozenset({999}), now=100 + 2 * 86400
        )
    ) == ()


def test_ascend_request_cannot_be_claimed_by_generic_approval(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("request-1", ascend_application(), remind_at=100)

    assert (
        storage.claim_pending_request(
            "request-1", forbidden_server="alibaba_ascend"
        )
        is None
    )
    assert storage.claim_pending_request(
        "request-1", required_server="alibaba_ascend"
    ) == ascend_application()


def test_failed_reminder_delivery_is_retried(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("request-1", ascend_application(), remind_at=100)

    class FailingBot:
        def __init__(self):
            self.attempts = 0

        async def send_message(self, chat_id, text, **kwargs):
            self.attempts += 1
            raise RuntimeError("Telegram unavailable")

    bot = FailingBot()
    assert asyncio.run(
        deliver_ascend_reminders(bot, storage, frozenset({999}), now=100)
    ) == ()
    assert asyncio.run(
        deliver_ascend_reminders(bot, storage, frozenset({999}), now=101)
    ) == ()
    assert bot.attempts == 2


def test_duplicate_active_ascend_request_is_rejected_and_can_retry_after_failure(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("request-1", ascend_application(), remind_at=100)

    with pytest.raises(PendingRequestExistsError):
        storage.save_pending_request(
            "request-2", ascend_application(), remind_at=100
        )

    claimed = storage.claim_pending_request(
        "request-1", required_server="alibaba_ascend"
    )
    assert claimed == ascend_application()
    storage.release_pending_request_claim("request-1")
    assert storage.claim_pending_request(
        "request-1", required_server="alibaba_ascend"
    ) == ascend_application()


def test_crashed_ascend_claim_becomes_retryable_after_lease(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("request-1", ascend_application(), remind_at=100)

    assert storage.claim_pending_request(
        "request-1", required_server="alibaba_ascend", now=100
    ) == ascend_application()
    assert (
        storage.claim_pending_request(
            "request-1", required_server="alibaba_ascend", now=3699
        )
        is None
    )
    assert storage.claim_pending_request(
        "request-1", required_server="alibaba_ascend", now=3700
    ) == ascend_application()


def test_reminder_worker_recovers_stale_processing_request(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("request-1", ascend_application(), remind_at=100)
    storage.claim_pending_request(
        "request-1", required_server="alibaba_ascend", now=100
    )

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    assert asyncio.run(
        deliver_ascend_reminders(bot, storage, frozenset({999}), now=3700)
    ) == ("request-1",)
