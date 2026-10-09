"""Выдача доступа: что делает скрипт на сервере базы, когда владелец нажал «Одобрить».

Прежний тест рядом со скриптом поднимал Postgres удалённой службы MCP и в обычном
прогоне не запускался вовсе (`testpaths = ["tests"]`). Служба удалена 07-10-2026, и
проверять в ней нечего: доступ теперь это учётка в GitLab, а разговор с GitLab
подменяется здесь целиком.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))


@pytest.fixture
def выдача(monkeypatch):
    monkeypatch.setenv("LAB_ACCESS_SITE", "gitlab")
    monkeypatch.setenv("LAB_GITLAB_TOKEN", "x" * 20)
    monkeypatch.setenv("LAB_GITLAB_PUBLIC_URL", "https://база.пример")
    модуль = importlib.reload(importlib.import_module("telegram_mcp_access"))
    запросы: list[tuple[str, str, dict | None]] = []
    люди: dict[str, dict] = {}

    def gitlab(путь, *, method="GET", payload=None):
        запросы.append((method, путь, payload))
        if путь.startswith("users?username="):
            логин = путь.split("=", 1)[1]
            return [люди[логин]] if логин in люди else []
        if путь.startswith("users?search="):
            почта = путь.split("=", 1)[1].replace("%40", "@")
            return [ч for ч in люди.values() if ч["email"] == почта]
        if путь == "users" and method == "POST":
            человек = {"id": 70 + len(люди), "username": payload["username"],
                       "email": payload["email"], "current_sign_in_at": None,
                       "last_sign_in_at": None}
            люди[человек["username"]] = человек
            return человек
        if путь.endswith("/personal_access_tokens") and method == "POST":
            return {"id": 5, "token": "glpat-" + "y" * 20}
        if путь.startswith("personal_access_tokens?user_id="):
            return [{"id": 5, "name": "git-abc123", "revoked": False},
                    {"id": 6, "name": "своим-ключом", "revoked": False}]
        if путь.startswith("personal_access_tokens/") and method == "DELETE":
            return None
        if method == "PUT":
            return {}
        raise AssertionError(f"неожидаемый вызов {method} {путь}")

    monkeypatch.setattr(модуль, "_gitlab", gitlab)
    return модуль, запросы, люди


@pytest.mark.parametrize("заявка, причина", [
    ({"action": "issue", "telegram_user_id": -5}, "Invalid Telegram ID"),
    ({"action": "issue", "telegram_user_id": 1.0}, "Invalid Telegram ID"),
    ({"action": "выдать", "telegram_user_id": 7}, "Unsupported action"),
    ({"action": "issue", "telegram_user_id": 7, "лишнее": 1}, "Unexpected request fields"),
    ({"action": "issue", "telegram_user_id": 7, "token": "короткий"}, "Invalid token"),
    ({"action": "issue", "telegram_user_id": 7, "role": "владелец"}, "Invalid role"),
])
def test_негодная_заявка_отвергается_с_причиной(выдача, заявка, причина):
    модуль, _, _ = выдача
    with pytest.raises(ValueError, match=причина):
        модуль.grant(заявка)


def test_новому_человеку_заводится_учётка_пароль_и_ключ(выдача):
    модуль, запросы, _ = выдача
    ответ = модуль.grant({"action": "issue", "telegram_user_id": 42, "token": "",
                          "username": "fu20", "full_name": "Тимофей Карягин"})
    assert ответ["site_login"] == "fu20"
    assert ответ["user_id"] == "70" and ответ["site_user_id"] == "70"
    assert ответ["email"] == "telegram-42@users.brainlab-ai.com"
    assert ответ["site_password"] and ответ["git_token"]
    assert ответ["site_url"] == "https://база.пример"
    # В корневую группу не записываем: разработчик корня это разработчик каждой работы.
    assert not [п for м, п, _ in запросы if м == "POST" and п.startswith("groups")]


def test_повторная_выдача_не_плодит_учёток_и_меняет_пароль_невходившему(выдача):
    модуль, запросы, _ = выдача
    первая = модуль.grant({"action": "issue", "telegram_user_id": 42, "token": "",
                           "username": "fu20", "full_name": "Т"})
    запросы.clear()
    вторая = модуль.grant({"action": "issue", "telegram_user_id": 42, "token": "",
                           "username": "fu20", "full_name": "Т"})
    assert вторая["user_id"] == первая["user_id"]
    assert not [п for м, п, _ in запросы if м == "POST" and п == "users"]
    # Человек ни разу не входил, значит прежним паролем никто не пользуется: без замены
    # он остался бы без входа, потому что почта на сервере выключена и «забыли пароль»
    # не работает. 04-10-2026 на этом встали две заявки.
    assert вторая["site_password"]
    assert [п for м, п, тело in запросы if м == "PUT" and тело and "password" in тело]


def test_входившему_пароль_не_меняют(выдача):
    модуль, запросы, люди = выдача
    модуль.grant({"action": "issue", "telegram_user_id": 42, "token": "", "username": "fu20"})
    люди["fu20"]["current_sign_in_at"] = "2026-10-09T10:00:00Z"
    запросы.clear()
    ответ = модуль.grant({"action": "issue", "telegram_user_id": 42, "token": "",
                          "username": "fu20"})
    assert ответ["site_password"] == ""
    assert not [п for м, п, тело in запросы if м == "PUT" and тело and "password" in тело]
    # Ключ для git выдаётся и на повторной заявке: без него участник ищет его сам.
    assert ответ["git_token"]


def test_отзыв_снимает_только_наши_ключи_и_не_блокирует_вход(выдача):
    модуль, запросы, _ = выдача
    модуль.grant({"action": "issue", "telegram_user_id": 42, "token": "", "username": "fu20"})
    запросы.clear()
    ответ = модуль.grant({"action": "revoke", "telegram_user_id": 42, "token": ""})
    assert ответ["revoked"] == 1
    assert ("DELETE", "personal_access_tokens/5", None) in запросы
    assert not [п for м, п, _ in запросы if м == "DELETE" and п.startswith("users")]
    assert not [п for м, п, тело in запросы if м == "POST" and "block" in п]


def test_отзыв_без_учётки_говорит_об_этом(выдача):
    модуль, _, _ = выдача
    with pytest.raises(ValueError, match="Учётки с таким телеграмом нет"):
        модуль.grant({"action": "revoke", "telegram_user_id": 42, "token": ""})
