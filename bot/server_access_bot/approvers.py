"""Кто может одобрять заявки и как их показать: подписи получателей, список
кандидатов в рекомендатели для формы и строка «кому ещё ушла заявка».

Вынесено из app.py, чтобы этим мог пользоваться и mcp_router без циклического
импорта (app импортирует mcp_router)."""

import html

from .storage import Storage


def approver_label(storage: Storage, telegram_user_id: int) -> str:
    """Имя и @ник получателя заявки, если бот их знает; иначе — telegram id."""
    profile = storage.profile_for(telegram_user_id)
    name = (profile.full_name or "").strip() if profile else ""
    username = (profile.telegram_username or "").strip().lstrip("@") if profile else ""
    if name and username:
        return f"{name} (@{username})"
    if name:
        return name
    if username:
        return f"@{username}"
    return f"id {telegram_user_id}"


def approver_label_map(
    storage: Storage, approvers: frozenset[int]
) -> dict[int, str]:
    return {approver_id: approver_label(storage, approver_id) for approver_id in approvers}


def approver_candidates(
    storage: Storage, approvers: frozenset[int]
) -> list[dict[str, object]]:
    """Выбор рекомендателей для формы: кто сейчас может одобрять (Maintainer корня),
    с человекочитаемой подписью. Отсортировано по подписи — для стабильного вида."""
    candidates = [
        {"id": approver_id, "label": approver_label(storage, approver_id)}
        for approver_id in approvers
    ]
    candidates.sort(key=lambda item: str(item["label"]).casefold())
    return candidates


def also_sent_line(labels: dict[int, str], *, exclude: int) -> str:
    """Строка «кому ещё ушла заявка» для одного получателя (пустая, если больше никому)."""
    others = sorted(label for approver_id, label in labels.items() if approver_id != exclude)
    if not others:
        return ""
    return "\n\n<i>Также отправлено на одобрение:</i> " + html.escape(", ".join(others))
