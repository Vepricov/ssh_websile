import json

import pytest
from aiogram.types import User

from server_access_bot.mcp_profile import parse_application
from server_access_bot.mcp_access import McpAccessService
from server_access_bot.storage import Storage


def test_application_uses_telegram_identity_and_the_server_profile_fields():
    user = User(id=42, is_bot=False, first_name="Alice", username="alice_tg")
    payload = dict(version=1, kind="mcp_request", full_name="Alice Smith",
                   username="alice", recommender="Professor", requested_role="manager",
                   activity="Literature", access_term="бессрочно", telegram_user_id=1)
    result = parse_application(user, json.dumps(payload))
    assert result["profile"].telegram_user_id == 42
    assert result["profile"].telegram_username == "alice_tg"
    assert result["profile"].full_name == "Alice Smith"
    assert result["profile"].recommender == "Professor"
    assert result["username"] == "alice"
    assert result["requested_role"] == "manager"
    with pytest.raises(ValueError):
        parse_application(user, json.dumps({**payload, "requested_role": "administrator"}))


def test_requested_role_is_not_a_grant_and_approver_can_choose_another(tmp_path):
    class Bridge:
        calls = []

        def issue(self, user_id, token, role="member", username="", full_name=""):
            self.calls.append(role)
            return {"email": "person@example.test", "role": role, "user_id": "user-42"}

    bridge = Bridge()
    service = McpAccessService(Storage(tmp_path / "db", b"a" * 32), bridge, frozenset({1}))
    row = service.request(42, "Alice Smith", username="alice", recommender="Professor",
                          telegram_username="alice_tg", requested_role="lead")
    assert row["requested_role"] == "lead"
    assert row["username"] == "alice"
    assert bridge.calls == []
    with pytest.raises(PermissionError):
        service.approve(42, row["request_id"], "lead")
    access = service.approve(1, row["request_id"], "manager")
    assert access["role"] == "manager"
    assert access["username"] == "alice"
    assert service.get(42)["approved_role"] == "manager"
    with pytest.raises(ValueError):
        service.approve(1, row["request_id"], "lead")


def test_existing_member_can_request_a_role_without_losing_current_access(tmp_path):
    class Bridge:
        def issue(self, user_id, token, role="member", username="", full_name=""):
            return {"email": "person@example.test", "role": role, "user_id": "user-42"}

    service = McpAccessService(Storage(tmp_path / "db", b"a" * 32), Bridge(), frozenset({1}))
    first = service.request(42, "Alice Smith", username="alice")
    access = service.approve(1, first["request_id"])
    service.delivered(first["request_id"])
    change = service.request(42, "Alice Smith", username="alice", requested_role="manager")
    assert change["status"] == "pending"
    assert change["request_id"] != first["request_id"]
    with pytest.raises(PermissionError):
        service.approve(42, change["request_id"], "manager")
    service.reject(1, change["request_id"])
    assert service.get(42)["status"] == "delivered"
    assert service.approve(1, change["request_id"])["token"] == access["token"]
    change = service.request(42, "Alice Smith", username="alice", requested_role="manager")
    upgraded = service.approve(1, change["request_id"], "manager")
    assert upgraded["token"] == access["token"]
    assert upgraded["role"] == "manager"
