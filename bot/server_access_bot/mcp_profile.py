"""MCP applications reuse the server profile, not its provisioning permissions."""

import json

from aiogram.types import User

from .domain import Profile
from .service import USERNAME_RE, validate_profile

FORM_URL = "https://vepricov.github.io/ssh_websile/mcp-form.html?v=access-cleanup-20261001"
#: Лестница ролей в лаборатории — это лестница ролей GitLab, и названия в скобках те же,
#: что человек увидит на странице состава. Четыре ступени приняты владельцем 17-10-2026.
#: Прежние имена (`member`, `manager`) понимаются, чтобы старые заявки не стали неизвестной
#: ролью: `member` это нынешний гость, `manager` — исследователь.
ROLES = {
    "guest": ("Гость (Guest)",
              "Видит дерево направлений, тем и работ и всю литературу. Содержимое работ "
              "закрыто. С этого начинают все."),
    "reporter": ("Студент (Reporter)",
                 "Читает все работы целиком: утверждения, прогоны, рукописи, обсуждения. "
                 "Писать нельзя, обсуждать можно."),
    "developer": ("Исследователь (Developer)",
                  "Права студента плюс работа: ветки и предложения в любой работе, свои "
                  "новые работы в любой теме. Сливать не может."),
    "lead": ("Лид (Maintainer)",
             "Сливает предложения во всех работах, заводит темы и владеет теми, где ведёт "
             "работу. Заявки в свои места решает сам."),
}
#: Что во что переводится, когда приходит заявка прежней формы.
PREZHNIE = {"member": "guest", "manager": "developer"}


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
    role = PREZHNIE.get(role, role) if isinstance(role, str) else role
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
