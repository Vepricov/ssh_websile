import json

import pytest
from aiogram.types import User

from server_access_bot.domain import Profile, ServerRequest
from server_access_bot.webapp import WebAppPayloadError, parse_web_app_data


def valid_payload() -> dict[str, object]:
    return {
        "version": 1,
        "kind": "server_request",
        "server_username": "alice",
        "access_term": "бессрочно",
        "activity": "fine-tuning",
        "servers": ["brain_lab", "vv_h200"],
    }


def test_profile_payload_uses_telegram_message_identity() -> None:
    user = User(id=4242, is_bot=False, first_name="Alice", username="alice_tg")
    submission = parse_web_app_data(
        user,
        json.dumps(
            {
                "version": 1,
                "kind": "profile",
                "full_name": "Alice Smith",
                "recommender": "Bob Jones",
                "telegram_user_id": 999,
            }
        ),
    )

    assert submission == Profile(
        telegram_user_id=4242,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        recommender="Bob Jones",
    )


def test_server_request_payload_uses_telegram_message_identity() -> None:
    user = User(id=4242, is_bot=False, first_name="Alice", username="alice_tg")

    submission = parse_web_app_data(user, json.dumps(valid_payload()))

    assert submission == ServerRequest(
        telegram_user_id=4242,
        telegram_username="alice_tg",
        server_username="alice",
        access_term="бессрочно",
        activity="fine-tuning",
        servers=("brain_lab", "vv_h200"),
    )


def test_malformed_web_app_payload_is_rejected() -> None:
    user = User(id=4242, is_bot=False, first_name="Alice")

    with pytest.raises(WebAppPayloadError, match="повторно"):
        parse_web_app_data(user, "not-json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("server_username", None),
        ("servers", "brain_lab"),
        ("kind", "unknown"),
    ],
)
def test_web_app_payload_shape_is_validated(field: str, value: object) -> None:
    user = User(id=4242, is_bot=False, first_name="Alice")
    payload = valid_payload()
    payload[field] = value

    with pytest.raises(WebAppPayloadError, match="Проверьте"):
        parse_web_app_data(user, json.dumps(payload))


def test_oversized_web_app_payload_is_rejected() -> None:
    user = User(id=4242, is_bot=False, first_name="Alice")
    payload = valid_payload()
    payload["activity"] = "x" * 4096

    with pytest.raises(WebAppPayloadError, match="слишком много"):
        parse_web_app_data(user, json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("server_username", "a" * 33),
        ("activity", "x" * 501),
        ("servers", ["brain_lab"] * 4),
    ],
)
def test_web_app_field_limits_are_enforced(field: str, value: object) -> None:
    user = User(id=4242, is_bot=False, first_name="Alice")
    payload = valid_payload()
    payload[field] = value

    with pytest.raises(WebAppPayloadError, match="Проверьте"):
        parse_web_app_data(user, json.dumps(payload))
