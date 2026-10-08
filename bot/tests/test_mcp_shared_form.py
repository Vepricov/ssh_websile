import asyncio
from datetime import datetime, timezone
import json

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User, WebAppData

from server_access_bot.app import build_router
from server_access_bot.domain import Application, Profile
from server_access_bot.mcp_access import McpAccessService
from server_access_bot.mcp_router import build_mcp_router
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
            return Message(message_id=len(self.sent), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text)
        return True


class Bridge:
    def __init__(self):
        self.calls = []

    def issue(self, user_id, token, role="member", username="", full_name=""):
        self.calls.append((user_id, role))
        return {"email": f"telegram-{user_id}@users.brainlab-ai.com", "role": role,
                "user_id": str(user_id), "telegram_user_id": user_id}


class Provisioner:
    def username_available(self, host, username):
        return True

    def provision(self, *args, **kwargs):
        raise AssertionError("No server may be provisioned by an MCP application")


def message(data):
    return Message(message_id=10, date=datetime.now(timezone.utc),
                   chat=Chat(id=42, type="private"),
                   from_user=User(id=42, is_bot=False, first_name="Alice", username="alice_tg"),
                   web_app_data=WebAppData(button_text="Получить доступ", data=json.dumps(data)))


def test_mcp_checkbox_in_server_form_creates_named_request_then_approved_role(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        storage.save_profile(Profile(42, "alice_tg", "Alice Smith", "Bob Jones"))
        bridge = Bridge()
        mcp = McpAccessService(storage, bridge, frozenset({1}))
        servers = AccessService(storage, Provisioner())
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_mcp_router(mcp))
        dispatcher.include_router(build_router(servers, object(), admin_user_ids=frozenset({1}), mcp_service=mcp))
        payload = dict(version=1, kind="server_request", server_username="alice",
                       access_term="", activity="", servers=["mcp"], mcp_role="manager",
                       telegram_user_id=1, full_name="Spoofed Name", recommender="Spoofed")
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(payload)))
        row = mcp.get(42)
        assert row and row["status"] == "pending"
        assert row["full_name"] == "Alice Smith" and row["recommender"] == "Bob Jones"
        assert row["username"] == "alice" and row["requested_role"] == "manager"
        assert bridge.calls == []
        notices = [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]
        assert len(notices) == 1 and "Alice Smith" in notices[0].text and "alice" in notices[0].text
        callback = CallbackQuery(id="approval", from_user=User(id=1, is_bot=False, first_name="Owner"),
                                 chat_instance="1", message=Message(message_id=11, date=datetime.now(timezone.utc),
                                 chat=Chat(id=1, type="private")), data=f"mcp:grant:member:{row['request_id']}")
        await dispatcher.feed_update(bot, Update(update_id=2, callback_query=callback))
        assert bridge.calls == [(42, "member")]
        assert mcp.get(42)["status"] == "delivered"
        delivery = next(x for x in telegram.sent if isinstance(x, SendMessage) and "Личный ключ:" in x.text)
        assert delivery.protect_content and delivery.chat_id == 42
        assert "alice" in delivery.text and "Участник" in delivery.text
    asyncio.run(scenario())


def test_combined_server_and_mcp_request_keeps_independent_approvals(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        storage.save_profile(Profile(42, "alice_tg", "Alice Smith", "Bob Jones"))
        bridge = Bridge()
        mcp = McpAccessService(storage, bridge, frozenset({1}))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_mcp_router(mcp))
        dispatcher.include_router(build_router(AccessService(storage, Provisioner()), object(),
                                               admin_user_ids=frozenset({1}), mcp_service=mcp))
        payload = dict(version=1, kind="server_request", server_username="alice",
                       access_term="бессрочно", activity="Research", servers=["mcp", "brain_lab"], mcp_role="lead")
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(payload)))
        assert mcp.get(42)["status"] == "pending" and bridge.calls == []
        notices = [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]
        assert len(notices) == 2
        server_notice = next(x for x in notices if any(b.callback_data.startswith("admin:approve:")
                             for row in x.reply_markup.inline_keyboard for b in row))
        button = server_notice.reply_markup.inline_keyboard[0][0]
        request_id = button.callback_data.rsplit(":", 1)[-1]
        application = storage.claim_pending_request(request_id)
        assert application.servers == ("brain_lab",)
        assert application.full_name == "Alice Smith"
    asyncio.run(scenario())


def test_invalid_role_or_missing_profile_cannot_create_any_access(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        bridge = Bridge()
        mcp = McpAccessService(storage, bridge, frozenset({1}))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(AccessService(storage, Provisioner()), object(),
                                               admin_user_ids=frozenset({1}), mcp_service=mcp))
        payload = dict(version=1, kind="server_request", server_username="alice", access_term="",
                       activity="", servers=["mcp"], mcp_role="lead")
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(payload)))
        assert mcp.get(42) is None
        assert any(isinstance(x, SendMessage) and "Мой профиль" in x.text for x in telegram.sent)
        storage.save_profile(Profile(42, "alice_tg", "Alice Smith", "Bob Jones"))
        payload["mcp_role"] = "admin"
        await dispatcher.feed_update(bot, Update(update_id=2, message=message(payload)))
        assert mcp.get(42) is None and bridge.calls == []
        assert not [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]
    asyncio.run(scenario())


def test_demo_resource_form_never_submits_a_real_mcp_request(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        bridge = Bridge()
        mcp = McpAccessService(storage, bridge, frozenset({1}))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(AccessService(storage, Provisioner()), object(),
                                               demo_user_ids=frozenset({42}), mcp_service=mcp))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
        await state.set_data({"demo": True, "demo_full_name": "Demo User", "demo_recommender": "Owner"})
        payload = dict(version=1, kind="server_request", server_username="alice", access_term="",
                       activity="", servers=["mcp"], mcp_role="lead")
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(payload)))
        assert mcp.get(42) is None and bridge.calls == [] and storage.profile_for(42) is None
        assert any(isinstance(x, SendMessage) and "DEMO · заявка MCP" in x.text for x in telegram.sent)
    asyncio.run(scenario())


def test_typed_mcp_application_gets_form_help_without_creating_request(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        bridge = Bridge()
        mcp = McpAccessService(storage, bridge, frozenset({1}))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_mcp_router(mcp))
        dispatcher.include_router(build_router(AccessService(storage, Provisioner()), object(),
                                               admin_user_ids=frozenset({1}), mcp_service=mcp))

        def text(value):
            return Message(message_id=10, date=datetime.now(timezone.utc),
                           chat=Chat(id=42, type="private"),
                           from_user=User(id=42, is_bot=False, first_name="Alice"), text=value)

        await dispatcher.feed_update(bot, Update(update_id=1, message=text("/mcp")))
        intro = telegram.sent[-1]
        assert "Нажмите «Заполнить заявку MCP»" in intro.text
        application = "Alice Smith\nalice\nBob Jones\nУчастник"
        await dispatcher.feed_update(bot, Update(update_id=2, message=text(application)))
        reply = telegram.sent[-1]
        assert "Заявка ещё не отправлена" in reply.text
        assert any(b.web_app for row in reply.reply_markup.keyboard for b in row)
        assert mcp.get(42) is None and bridge.calls == []
        assert not [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]
        assert storage.profile_for(42) is None

        await dispatcher.feed_update(bot, Update(update_id=3, message=text("/cancel")))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=42, user_id=42)
        assert await state.get_state() is None
        count = len(telegram.sent)
        await dispatcher.feed_update(bot, Update(update_id=4, message=text(application)))
        assert len(telegram.sent) == count
    asyncio.run(scenario())


def test_server_form_splits_alibaba_into_its_own_admin_request(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        storage.save_profile(Profile(42, "alice_tg", "Alice Smith", "Bob Jones"))
        mcp = McpAccessService(storage, Bridge(), frozenset({1}))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(
            AccessService(storage, Provisioner(), ascend_group_url="https://t.me/+test"),
            object(), admin_user_ids=frozenset({1}), mcp_service=mcp))
        payload = dict(version=1, kind="server_request", server_username="alice",
                       access_term="бессрочно", activity="", servers=["brain_lab", "alibaba_ascend"])
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(payload)))
        notices = [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]
        assert len(notices) == 2
        assert "Заявка на Alibaba" in notices[1].text
        claimed = []
        for notice in notices:
            request_id = notice.reply_markup.inline_keyboard[0][0].callback_data.rsplit(":", 1)[-1]
            claimed.append(storage.claim_pending_request(request_id).servers)
        assert claimed == [("brain_lab",), ("alibaba_ascend",)]
    asyncio.run(scenario())


def test_known_user_gets_two_a100_only_after_admin_approval(tmp_path):
    async def scenario():
        storage = Storage(tmp_path / "db", b"a" * 32)
        storage.save_profile(Profile(42, "alice_tg", "Alice Smith", "Bob Jones"))
        storage.save_application(Application(42, "alice_tg", "Alice Smith", "alice",
                                              "бессрочно", "Bob Jones", "", ("brain_lab",)))
        mcp = McpAccessService(storage, Bridge(), frozenset({1}))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram)
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(AccessService(storage, Provisioner()), object(),
                                               admin_user_ids=frozenset({1}), mcp_service=mcp))
        payload = dict(version=1, kind="server_request", server_username="alice",
                       access_term="бессрочно", activity="", servers=["2_a100_brain"])
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(payload)))
        notices = [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]
        assert len(notices) == 1 and "Заявка на 2 × A100" in notices[0].text
        request_id = notices[0].reply_markup.inline_keyboard[0][0].callback_data.rsplit(":", 1)[-1]
        assert storage.claim_pending_request(request_id).servers == ("2_a100_brain",)
    asyncio.run(scenario())
