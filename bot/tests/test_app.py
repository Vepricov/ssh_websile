from server_access_bot.app import (
    MAIN_MENU,
    application_from_request,
    available_servers_text,
    confirmation_keyboard,
    my_info_text,
    registry_confirmation_keyboard,
    registry_confirmation_text,
    registry_link_is_trusted,
    server_dashboard_keyboard,
    server_dashboard_text,
    server_request_menu,
)
from server_access_bot.domain import (
    Profile,
    ProvisionedAccess,
    RegistryMatch,
    ServerRequest,
)


def registry_match() -> RegistryMatch:
    return RegistryMatch(
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="fine-tuning",
        servers=("brain_lab", "yandex"),
        source_row_count=2,
        telegram_user_id=101,
    )


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


def test_registry_services_are_shown_without_fake_password_or_add_button() -> None:
    match = registry_match()

    text = server_dashboard_text([], match)
    keyboard = server_dashboard_keyboard([], match.servers)

    assert "Доступ найден в таблице" in text
    assert "Пароль не сохранён ботом" in text
    assert "Ник: <code>alice</code>" in text
    assert keyboard is not None
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
    assert "servers:add:brain_lab" not in callbacks
    assert "servers:add:shkodnik_opt" in callbacks


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


def test_main_menu_exposes_profile_catalog_info_and_access_actions() -> None:
    labels = [[button.text for button in row] for row in MAIN_MENU.keyboard]

    assert labels == [
        ["👤 Мой профиль", "🔐 Мои серверы"],
        ["🖥 Доступные серверы", "➕ Получить доступ", "📚 Сайт и правила"],
    ]
    access_button = MAIN_MENU.keyboard[1][1]
    assert access_button.web_app is not None
    assert access_button.web_app.url == (
        "https://vepricov.github.io/ssh_websile/server-form.html"
    )


def test_registry_link_requires_one_tap_confirmation() -> None:
    match = registry_match()

    text = registry_confirmation_text(match)
    keyboard = registry_confirmation_keyboard("link-123")

    assert "Alice Smith" in text
    assert "Новый (BrainLab)" in text
    assert "Яндекс-сервер" in text
    assert keyboard.inline_keyboard[0][0].callback_data == ("registry:confirm:link-123")


def test_registry_link_requires_matching_numeric_telegram_id() -> None:
    trusted = registry_match()
    legacy = RegistryMatch(
        telegram_username=trusted.telegram_username,
        full_name=trusted.full_name,
        server_username=trusted.server_username,
        access_term=trusted.access_term,
        recommender=trusted.recommender,
        activity=trusted.activity,
        servers=trusted.servers,
        source_row_count=trusted.source_row_count,
    )

    assert registry_link_is_trusted(trusted, 101)
    assert not registry_link_is_trusted(trusted, 202)
    assert not registry_link_is_trusted(legacy, 101)


def test_my_info_combines_profile_and_registered_services() -> None:
    profile = Profile(101, "alice_tg", "Alice <Smith>", "Bob Jones")

    text = my_info_text(profile, registry_match(), [])

    assert "Alice &lt;Smith&gt;" in text
    assert "<b>Мой профиль</b>" in text
    assert "@alice_tg" in text
    assert "Ник на серверах: <code>alice</code>" in text
    assert "Новый (BrainLab)" in text
    assert "Яндекс-сервер" in text


def test_available_server_message_is_complete() -> None:
    catalog = available_servers_text()
    assert "Старый (mipt)" in catalog
    assert "Новый (BrainLab)" in catalog
    assert "H200 (VV)" in catalog
    assert '<a href="https://t.me/aloeme">@aloeme</a>' in catalog
