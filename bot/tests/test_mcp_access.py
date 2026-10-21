import asyncio
import json

import pytest

from server_access_bot.mcp_access import McpAccessService
from server_access_bot.storage import Storage


class Bridge:
    def __init__(self):
        self.calls = []

    def issue(self, user_id, token, role="member", username="", full_name=""):
        self.calls.append((user_id, token))
        return {"email": f"telegram-{user_id}@users.brainlab-ai.com", "role": role}

    def revoke(self, user_id):
        self.calls.append(("revoke", user_id))


def test_approval_is_required_and_retry_keeps_the_same_identity_and_secret(tmp_path):
    storage = Storage(tmp_path / "bot.sqlite3", b"a" * 32)
    bridge = Bridge()
    service = McpAccessService(storage, bridge, frozenset({1}))
    request = service.request(42, "Alice Smith", "Project A")
    assert bridge.calls == []
    assert service.request(42, "Changed name", "Other project")["request_id"] == request["request_id"]
    with pytest.raises(PermissionError):
        service.approve(42, request["request_id"])
    first = service.approve(1, request["request_id"])
    second = service.approve(1, request["request_id"])
    assert first["token"] == second["token"]
    assert len(bridge.calls) == 1
    assert first["telegram_user_id"] == 42
    assert first["role"] == "guest"
    assert first["token"].encode() not in storage.path.read_bytes()


def test_redelivery_of_a_grant_stored_under_a_former_role_name(tmp_path):
    storage = Storage(tmp_path / "bot.sqlite3", b"a" * 32)
    service = McpAccessService(storage, Bridge(), frozenset({1}))
    request = service.request(42, "Alice", "Literature")
    service.approve(1, request["request_id"])
    with storage._connect() as db:
        db.execute("UPDATE mcp_requests SET approved_role='member', status='delivered', "
                   "account=json_set(account, '$.role', 'member') WHERE request_id=?",
                   (request["request_id"],))
    access = service.approve(1, request["request_id"])
    assert access["role"] == "guest"


def test_failed_remote_reply_can_be_retried_after_restart(tmp_path):
    class LostReply(Bridge):
        def issue(self, user_id, token, role="member", username="", full_name=""):
            result = super().issue(user_id, token, role)
            if len(self.calls) == 1:
                raise TimeoutError("reply lost after commit")
            return result

    storage = Storage(tmp_path / "bot.sqlite3", b"a" * 32)
    bridge = LostReply()
    service = McpAccessService(storage, bridge, frozenset({1}))
    row = service.request(42, "Alice", "Literature")
    with pytest.raises(TimeoutError):
        service.approve(1, row["request_id"])
    restarted = McpAccessService(Storage(storage.path, b"a" * 32), bridge, frozenset({1}))
    assert len(restarted.pending(1)) == 1
    access = restarted.approve(1, row["request_id"])
    assert bridge.calls[0] == bridge.calls[1]
    assert "token" not in restarted.get(42)
    assert "secret" not in restarted.get(42)
    restarted.delivered(access["request_id"])
    assert restarted.pending(1) == []


def test_rejection_revocation_and_new_request_preserve_authority(tmp_path):
    bridge = Bridge()
    service = McpAccessService(Storage(tmp_path / "db", b"a" * 32), bridge, frozenset({1}))
    row = service.request(42, "Alice", "Literature")
    assert service.reject(1, row["request_id"]) == 42
    with pytest.raises(ValueError):
        service.approve(1, row["request_id"])
    new = service.request(42, "Alice", "Literature")
    assert new["request_id"] != row["request_id"]
    old_key = service.approve(1, new["request_id"])["token"]
    with pytest.raises(PermissionError):
        service.revoke(43, 42)
    with pytest.raises(ValueError):
        service.reject(1, new["request_id"])
    service.revoke(42, 42)
    assert service.get(42)["status"] == "revoked"
    again = service.request(42, "Alice", "Literature")
    assert service.approve(1, again["request_id"])["token"] != old_key


def test_no_configuration_means_no_request_or_grant(tmp_path):
    bridge = Bridge()
    service = McpAccessService(Storage(tmp_path / "db", b"a" * 32), bridge, frozenset())
    with pytest.raises(RuntimeError):
        service.request(42, "Alice", "Literature")
    assert bridge.calls == []


def test_ssh_transports_secret_only_on_stdin(monkeypatch):
    from server_access_bot.mcp_access import McpBridge
    from subprocess import CompletedProcess

    token = "a" * 64
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        return CompletedProcess(args, 0, '{"role":"guest","email":"telegram-42@users.brainlab-ai.com","user_id":"user-42","telegram_user_id":42}', "")

    monkeypatch.setattr("server_access_bot.mcp_access.subprocess.run", run)
    McpBridge().issue(42, token)
    assert token not in " ".join(seen[0][0])
    assert token in seen[0][1]["input"]
    assert seen[0][1]["capture_output"]


@pytest.mark.parametrize("notification_fails", [False, True])
def test_bot_routes_request_approval_and_protected_delivery(tmp_path, notification_fails):
    from datetime import datetime, timezone
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User, WebAppData
    from server_access_bot.mcp_router import build_mcp_router

    class Telegram(BaseSession):
        def __init__(self):
            super().__init__()
            self.sent = []
            self.notification_failed = False

        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            yield b""

        async def make_request(self, bot, method, timeout=None):
            self.sent.append(method)
            if (notification_fails and isinstance(method, SendMessage)
                    and method.chat_id == 1 and not self.notification_failed):
                self.notification_failed = True
                raise RuntimeError("Simulated notification failure")
            if isinstance(method, SendMessage):
                return Message(message_id=len(self.sent), date=datetime.now(timezone.utc),
                               chat=Chat(id=method.chat_id, type="private"), text=method.text)
            return True

    async def scenario():
        bridge = Bridge()
        service = McpAccessService(Storage(tmp_path / "db", b"a" * 32), bridge, frozenset({1}))
        telegram = Telegram()
        bot = Bot("123456:abcdef", session=telegram, default=DefaultBotProperties(parse_mode="HTML"))
        dispatcher = Dispatcher()
        dispatcher.include_router(build_mcp_router(service, "https://t.me/example_testing"))

        def message(user_id, text):
            return Message(message_id=10, date=datetime.now(timezone.utc),
                           chat=Chat(id=user_id, type="private"),
                           from_user=User(id=user_id, is_bot=False, first_name="Alice"), text=text)

        def callback(user_id, data):
            return CallbackQuery(id="callback", from_user=User(id=user_id, is_bot=False, first_name="Owner" if user_id == 1 else "Alice"),
                                 chat_instance="1", message=message(user_id, "request"), data=data)

        await dispatcher.feed_update(bot, Update(update_id=1, message=message(42, "/mcp")))
        intro = next(x for x in telegram.sent if isinstance(x, SendMessage))
        assert "Для какого проекта" not in intro.text
        assert "Напишите одним сообщением" not in intro.text
        assert "https://brain-lab-research.github.io/claude-brainlab/lab-showcase/" in intro.text
        assert any(button.web_app and "/mcp-form.html" in button.web_app.url
                   for row in intro.reply_markup.keyboard for button in row)
        assert service.get(42) is None
        submission = message(42, "")
        submission = submission.model_copy(update={"web_app_data": WebAppData(button_text="MCP", data=json.dumps(dict(
            version=1, kind="mcp_request", full_name="Alice Smith", username="alice", recommender="Professor",
            requested_role="member", activity="", access_term="бессрочно", telegram_user_id=1)))})
        await dispatcher.feed_update(bot, Update(update_id=2, message=submission))
        row = service.get(42)
        assert row["status"] == "pending"
        assert row["purpose"] == ""
        assert bridge.calls == []
        notices = [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]
        assert len(notices) == 1
        assert "Alice" in notices[0].text and "42" in notices[0].text
        assert any(b.callback_data == f"mcp:grant:guest:{row['request_id']}"
                   for line in notices[0].reply_markup.inline_keyboard for b in line)
        await dispatcher.feed_update(bot, Update(update_id=6, callback_query=callback(42, "mcp:request")))
        assert service.get(42)["request_id"] == row["request_id"]
        assert len([x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1]) == 1

        if notification_fails:
            assert any(isinstance(x, SendMessage) and "уведомление Андрею не доставлено" in x.text
                       for x in telegram.sent)
            await dispatcher.feed_update(bot, Update(update_id=7, message=message(1, "/mcp_requests")))
            recovered = [x for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1][-1]
            assert any(b.callback_data == f"mcp:grant:guest:{row['request_id']}"
                       for line in recovered.reply_markup.inline_keyboard for b in line)

        await dispatcher.feed_update(bot, Update(update_id=3, callback_query=callback(43, f"mcp:grant:guest:{row['request_id']}")))
        assert bridge.calls == []
        await dispatcher.feed_update(bot, Update(update_id=4, callback_query=callback(1, f"mcp:grant:guest:{row['request_id']}")))
        assert service.get(42)["status"] == "delivered"
        delivery = next(x for x in telegram.sent if isinstance(x, SendMessage) and "Доступ в базу знания лаборатории выдан" in x.text)
        assert delivery.chat_id == 42
        assert delivery.protect_content is True
        assert "https://t.me/example_testing" in delivery.text
        assert "Личный ключ:" not in " ".join(x.text for x in telegram.sent if isinstance(x, SendMessage) and x.chat_id == 1)
        await dispatcher.feed_update(bot, Update(update_id=5, callback_query=callback(42, "mcp:show")))
        assert len(bridge.calls) == 1

    asyncio.run(scenario())


def test_simultaneous_button_requests_create_one_application(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    storage = Storage(tmp_path / "db", b"a" * 32)
    service = McpAccessService(storage, Bridge(), frozenset({1}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = list(pool.map(lambda _: service.request(42, "Alice"), range(2)))
    assert len({row["request_id"] for row in requests}) == 1
    assert sum(row["created"] for row in requests) == 1
    assert len(service.pending(1)) == 1
    assert service.get(42)["purpose"] == ""
