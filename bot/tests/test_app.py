from server_access_bot.app import (
    MAIN_MENU,
    application_from_request,
    confirmation_keyboard,
    server_dashboard_keyboard,
    server_dashboard_text,
    server_request_menu,
)
from server_access_bot.domain import Profile, ProvisionedAccess, ServerRequest


def test_server_request_is_combined_with_saved_profile() -> None:
    application = application_from_request(
        Profile(
            telegram_user_id=4242,
            telegram_username="alice_tg",
            full_name="Alice Smith",
            recommender="Bob Jones",
        ),
        ServerRequest(
            telegram_user_id=4242,
            telegram_username="alice_tg",
            server_username="alice",
            access_term="бессрочно",
            activity="fine-tuning",
            servers=("brain_lab",),
        ),
    )

    assert application.telegram_user_id == 4242
    assert application.telegram_username == "alice_tg"


def test_server_dashboard_contains_managed_accesses_and_yandex_contact() -> None:
    accesses = [
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
    text = server_dashboard_text(accesses)
    keyboard = server_dashboard_keyboard(accesses)

    assert "brain-secret" in text
    assert "h200-secret" in text
    assert "Старый (mipt)" in text
    assert "Доступ ещё не добавлен" in text
    assert "получите VPN" in text
    assert '<a href="https://t.me/aloeme">@aloeme</a>' in text
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].callback_data == "servers:add:shkodnik_opt"


def test_add_server_button_opens_prefilled_server_form() -> None:
    menu = server_request_menu("brain_lab")

    button = menu.keyboard[0][0]
    assert button.web_app is not None
    assert button.web_app.url.endswith("/server-form.html?server=brain_lab")


def test_confirmation_buttons_are_bound_to_one_request() -> None:
    keyboard = confirmation_keyboard("request-123")

    assert keyboard.inline_keyboard[0][0].callback_data == ("form:submit:request-123")
    assert keyboard.inline_keyboard[1][0].callback_data == ("form:cancel:request-123")


def test_untrusted_group_url_is_not_rendered_as_a_link() -> None:
    text = server_dashboard_text(
        [
            ProvisionedAccess(
                host="brain_lab",
                username="alice",
                password="secret",
                connection="ssh brain",
                group_url="javascript:alert(1)",
                created=True,
            )
        ]
    )

    assert 'href="javascript:' not in text
    assert "javascript:alert(1)" in text


def test_main_menu_has_separate_profile_and_server_forms() -> None:
    profile_button = MAIN_MENU.keyboard[0][0]
    server_button = MAIN_MENU.keyboard[1][0]

    assert profile_button.web_app is not None
    assert profile_button.web_app.url == (
        "https://vepricov.github.io/ssh_websile/access-form.html"
    )
    assert server_button.web_app is not None
    assert server_button.web_app.url == (
        "https://vepricov.github.io/ssh_websile/server-form.html"
    )
    assert MAIN_MENU.keyboard[2][0].text == "Мои серверы"
