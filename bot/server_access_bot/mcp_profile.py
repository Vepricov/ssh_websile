"""MCP applications reuse the server profile, not its provisioning permissions."""

import json

from aiogram.types import User

from .domain import Profile
from .service import USERNAME_RE, validate_profile

FORM_URL = "https://vepricov.github.io/ssh_websile/mcp-form.html?v=access-cleanup-20261001"
ROLES = {
    "member": ("Участник", "Читает общую базу, создаёт свои проекты; пишет в чужие после приглашения."),
    "manager": ("Координатор", "Права участника плюс приглашение людей и управление ключами участников. Не получает запись во все проекты."),
    "lead": ("Ведущий лаборатории", "Управляет всеми проектами, доступами и глобальными ролями."),
}


def is_application(raw: str) -> bool:
    try:
        data = json.loads(raw)
        return isinstance(data, dict) and data.get("kind") == "mcp_request"
    except (ValueError, TypeError):
        return False


def parse_application(user: User, raw: str) -> dict:
    if len(raw.encode("utf-8")) > 4096:
        raise ValueError("Сократите текст в форме.")
    data = json.loads(raw)
    fields = {"full_name": 120, "username": 32, "recommender": 120, "activity": 500}
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or data.get("kind") != "mcp_request"
            or any(not isinstance(data.get(key), str) or len(data[key].strip()) > limit
                   for key, limit in fields.items())):
        raise ValueError("Проверьте поля заявки и отправьте форму повторно.")
    role = data.get("requested_role")
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError("Выберите роль из списка.")
    if data.get("access_term") != "бессрочно":
        raise ValueError("Сейчас доступ к MCP действует до отзыва ключа.")
    username = data["username"].strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Ник: строчные латинские буквы, цифры, _ или -, до 32 символов.")
    profile = Profile(user.id, user.username, data["full_name"].strip(), data["recommender"].strip())
    validate_profile(profile)
    return {"profile": profile, "username": username, "requested_role": role,
            "activity": data["activity"].strip()}
