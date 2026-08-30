from aiogram.types import User

from server_access_bot.app import MAIN_MENU, application_from_data, credentials_text
from server_access_bot.domain import ProvisionedAccess


def test_form_identity_comes_from_numeric_telegram_user_id() -> None:
    user = User(id=4242, is_bot=False, first_name="Alice", username="alice_tg")
    application = application_from_data(
        user,
        {
            "full_name": "Alice Smith",
            "server_username": "alice",
            "access_term": "бессрочно",
            "recommender": "Bob Jones",
            "activity": "fine-tuning",
            "servers": ["brain_lab"],
        },
    )

    assert application.telegram_user_id == 4242
    assert application.telegram_username == "alice_tg"


def test_credentials_message_contains_each_server_password() -> None:
    text = credentials_text(
        [
            ProvisionedAccess(
                host="brain_lab",
                username="alice",
                password="brain-secret",
                connection="ssh brain",
                group_url="https://t.me/brain",
                created=True,
            ),
            ProvisionedAccess(
                host="vv_h200",
                username="alice",
                password="h200-secret",
                connection="ssh h200",
                group_url="https://t.me/h200",
                created=True,
            ),
        ]
    )

    assert "brain-secret" in text
    assert "h200-secret" in text
    assert "получите VPN" in text


def test_main_menu_opens_access_form_as_telegram_mini_app() -> None:
    button = MAIN_MENU.keyboard[0][0]

    assert button.web_app is not None
    assert button.web_app.url == (
        "https://vepricov.github.io/ssh_websile/access-form.html"
    )
