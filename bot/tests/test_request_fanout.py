import asyncio
from datetime import datetime, timezone
import json

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
    WebAppData,
)

from server_access_bot.app import build_router
from server_access_bot.domain import Profile
from server_access_bot.service import AccessService
from server_access_bot.storage import Storage


class Telegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.sent),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True


class Provisioner:
    def username_available(self, host, username):
        return True

    def provision(self, *args, **kwargs):
        raise AssertionError("A rejected request must never be provisioned")


def _form_message(data):
    return Message(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Alice", username="alice_tg"),
        web_app_data=WebAppData(button_text="Получить доступ", data=json.dumps(data)),
    )


def test_request_messages_roundtrip(tmp_path):
    storage = Storage(tmp_path / "db", b"a" * 32)
    storage.save_request_message("req-1", 1, 100)
    storage.save_request_message("req-1", 2, 200)
    storage.save_request_message("req-1", 2, 205)  # same chat updates message id
    storage.save_request_message("req-2", 3, 300)

    assert sorted(storage.request_messages("req-1")) == [(1, 100), (2, 205)]
    assert storage.request_messages("req-2") == [(3, 300)]

    storage.clear_request_messages("req-1")
    assert storage.request_messages("req-1") == []
    assert storage.request_messages("req-2") == [(3, 300)]


def test_resolving_a_request_clears_the_other_approvers_message(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        storage.save_profile(Profile(42, "alice_tg", "Alice Smith", "Bob Jones"))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(
            build_router(
                AccessService(storage, Provisioner()),
                object(),
                admin_user_ids=frozenset({1, 2}),
            )
        )
        payload = dict(
            version=1,
            kind="server_request",
            server_username="alice",
            access_term="бессрочно",
            activity="Research",
            servers=["brain_lab"],
        )
        await dispatcher.feed_update(
            bot, Update(update_id=1, message=_form_message(payload))
        )

        # Both admins got the request, and the bot remembered where it went.
        notices = [
            x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id in {1, 2}
        ]
        assert {x.chat_id for x in notices} == {1, 2}
        button = notices[0].reply_markup.inline_keyboard[0][0]
        request_id = button.callback_data.rsplit(":", 1)[-1]
        stored = dict(storage.request_messages(request_id))
        assert set(stored) == {1, 2}

        # Admin 1 rejects from their own message.
        callback = CallbackQuery(
            id="reject",
            from_user=User(id=1, is_bot=False, first_name="Owner"),
            chat_instance="1",
            message=Message(
                message_id=stored[1],
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type="private"),
            ),
            data=f"admin:reject:{request_id}",
        )
        await dispatcher.feed_update(
            bot, Update(update_id=2, callback_query=callback)
        )

        # Admin 2's dangling message was rewritten and its buttons removed.
        edits = [x for x in telegram.sent if isinstance(x, EditMessageText)]
        other = next(e for e in edits if e.chat_id == 2)
        assert other.message_id == stored[2]
        assert "отклонил" in other.text
        assert other.reply_markup is None

        # The applicant was told, and the fan-out bookkeeping is cleared.
        assert any(
            isinstance(x, SendMessage) and x.chat_id == 42 and "отклонена" in x.text
            for x in telegram.sent
        )
        assert storage.request_messages(request_id) == []

    asyncio.run(scenario())
