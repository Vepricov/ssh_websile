import json

from aiogram.types import User

from .domain import Application

FIELD_LIMITS = {
    "full_name": 120,
    "server_username": 32,
    "access_term": 64,
    "recommender": 120,
    "activity": 500,
}


class WebAppPayloadError(ValueError):
    pass


def _invalid_payload() -> WebAppPayloadError:
    return WebAppPayloadError("Проверьте поля формы и отправьте её повторно.")


def parse_web_app_data(user: User, raw_data: str) -> Application:
    if len(raw_data.encode("utf-8")) > 4096:
        raise WebAppPayloadError(
            "В форме слишком много текста. Сократите ответы и повторите."
        )
    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as error:
        raise WebAppPayloadError(
            "Форма повреждена. Откройте её и заполните повторно."
        ) from error
    if not isinstance(data, dict):
        raise _invalid_payload()
    if type(data.get("version")) is not int or data["version"] != 1:
        raise _invalid_payload()
    required_strings = (
        "full_name",
        "server_username",
        "access_term",
        "recommender",
    )
    if any(not isinstance(data.get(field), str) for field in required_strings):
        raise _invalid_payload()
    if not isinstance(data.get("activity", ""), str):
        raise _invalid_payload()
    if any(
        len(data.get(field, "").strip()) > limit
        for field, limit in FIELD_LIMITS.items()
    ):
        raise _invalid_payload()
    servers = data.get("servers")
    if not isinstance(servers, list) or any(
        not isinstance(host, str) for host in servers
    ):
        raise _invalid_payload()
    if len(servers) > 3 or len(servers) != len(set(servers)):
        raise _invalid_payload()
    return Application(
        telegram_user_id=user.id,
        telegram_username=user.username,
        full_name=data["full_name"].strip(),
        server_username=data["server_username"].strip(),
        access_term=data["access_term"].strip(),
        recommender=data["recommender"].strip(),
        activity=data.get("activity", "").strip(),
        servers=tuple(servers),
    )
