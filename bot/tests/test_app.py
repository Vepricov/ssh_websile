from server_access_bot.app import (
    BOT_DEFAULTS,
    MAIN_MENU,
    admin_approval_keyboard,
    ascend_admin_keyboard,
    application_from_request,
    available_servers_keyboard,
    available_servers_text,
    confirmation_keyboard,
    demo_accesses,
    gpu_force_keyboard,
    gpu_stop_confirmation_keyboard,
    main_menu,
    my_info_text,
    onboarding_text,
    reconcile_google_sheets,
    registry_confirmation_keyboard,
    registry_confirmation_text,
    registry_link_can_be_claimed,
    registry_link_is_trusted,
    request_requires_admin,
    server_dashboard_keyboard,
    server_dashboard_text,
    server_form_url,
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


def test_sheet_reconciliation_replays_users_and_requests() -> None:
    profile = Profile(101, "alice_tg", "Alice Smith", "Bob Jones")
    application = application_from_request(
        profile,
        ServerRequest(
            telegram_user_id=101,
            telegram_username="alice_tg",
            server_username="alice",
            access_term="бессрочно",
            activity="fine-tuning",
            servers=("brain_lab",),
        ),
    )

    class FakeStorage:
        def sheet_user_records(self):
            return ((profile, "Профиль заполнен", ()),)

        def sheet_request_records(self):
            return (("request-1", application, "Ожидает одобрения"),)

    class FakeSheets:
        def __init__(self) -> None:
            self.calls = []

        def sync_snapshot(self, users, requests) -> None:
            self.calls.append((users, requests))

    sheets = FakeSheets()

    reconcile_google_sheets(FakeStorage(), sheets)

    assert sheets.calls == [
        (
            ((profile, "Профиль заполнен", ()),),
            (("request-1", application, "Ожидает одобрения"),),
        )
    ]


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
    assert "Старый сервер (MIPT)" in text
    assert "Доступ ещё не добавлен" in text
    assert "получите VPN" in text
    assert "@undef1ne5" in text
    assert "helpdesk02@vkusvill.ru" in text
    assert "Получить доступ" in text
    assert "@shkodnik1917" not in text
    assert '<a href="https://t.me/aloeme">@aloeme</a>' in text
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].callback_data == "servers:add:shkodnik_opt"


def test_registry_services_are_shown_without_fake_password_or_add_button() -> None:
    match = registry_match()

    text = server_dashboard_text([], match)
    keyboard = server_dashboard_keyboard([], match.servers)

    assert "Доступ найден в таблице" in text
    assert "Пароль: ищите в переписке с @shkodnik1917" in text
    assert "Ник: <code>alice</code>" in text
    assert keyboard is not None
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
    assert "servers:add:brain_lab" not in callbacks
    assert "servers:add:shkodnik_opt" in callbacks


def test_ascend_access_is_shown_with_group_and_without_password() -> None:
    access = ProvisionedAccess(
        host="alibaba_ascend",
        username="alice",
        password=None,
        connection="Доступ выдаётся вручную через @fckand1",
        group_url="https://t.me/+test-only",
        created=False,
    )

    text = server_dashboard_text([access])
    keyboard = server_dashboard_keyboard([access])

    assert "✅ <b>Alibaba Ascend</b>" in text
    assert "@fckand1" in text
    assert 'href="https://t.me/+test-only"' in text
    assert "Пароль" not in text.split("<b>Alibaba Ascend</b>", 1)[1]
    assert keyboard is not None
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "servers:add:alibaba_ascend" not in callbacks


def test_registry_dashboard_does_not_show_invalid_legacy_unix_name() -> None:
    match = RegistryMatch(
        telegram_username="igor_vsh",
        full_name="Игорь Шалыгин",
        server_username="",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="research",
        servers=("brain_lab",),
        source_row_count=1,
        telegram_user_id=101,
    )

    text = server_dashboard_text([], match)

    assert "Ник: не указан в таблице" in text


def test_add_server_button_opens_prefilled_server_form() -> None:
    menu = server_request_menu("brain_lab")

    button = menu.keyboard[0][0]
    assert button.web_app is not None
    assert button.web_app.url.endswith("/server-form.html?server=brain_lab")


def test_server_form_url_prefills_username_and_existing_accesses() -> None:
    url = server_form_url(
        server_username="alice",
        existing_services=("shkodnik_opt", "brain_lab", "yandex"),
        requested_server="vv_h200",
    )

    assert url == (
        "https://vepricov.github.io/ssh_websile/server-form.html?"
        "server=vv_h200&username=alice&existing=shkodnik_opt%2Cbrain_lab%2Cyandex"
    )


def test_mlsub_add_button_appears_only_after_brain_lab_access() -> None:
    without_brain_lab = server_dashboard_keyboard([], ())
    with_brain_lab = server_dashboard_keyboard([], ("brain_lab",))

    assert without_brain_lab is not None
    assert with_brain_lab is not None
    callbacks_without = [
        button.callback_data
        for row in without_brain_lab.inline_keyboard
        for button in row
    ]
    callbacks_with = [
        button.callback_data for row in with_brain_lab.inline_keyboard for button in row
    ]
    assert "servers:add:mlsub" not in callbacks_without
    assert "servers:add:mlsub" in callbacks_with


def test_confirmation_buttons_are_bound_to_one_request() -> None:
    keyboard = confirmation_keyboard("request-123")

    assert keyboard.inline_keyboard[0][0].callback_data == ("form:submit:request-123")
    assert keyboard.inline_keyboard[1][0].callback_data == ("form:cancel:request-123")


def test_admin_approval_is_bound_to_a_durable_request() -> None:
    keyboard = admin_approval_keyboard("request-123")

    assert keyboard.inline_keyboard[0][0].callback_data == "admin:approve:request-123"
    assert keyboard.inline_keyboard[0][1].callback_data == "admin:reject:request-123"


def test_ascend_admin_keyboard_opens_prefilled_chat_and_keeps_durable_actions() -> None:
    keyboard = ascend_admin_keyboard("request-123", "Александр Мясников")

    contact = keyboard.inline_keyboard[0][0]
    assert contact.url == (
        "https://t.me/fckand1?text="
        "%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82%21%20%D0%94%D0%BE%D0%B1%D0%B0%D0%B2%D1%8C%2C%20"
        "%D0%BF%D0%BE%D0%B6%D0%B0%D0%BB%D1%83%D0%B9%D1%81%D1%82%D0%B0%2C%20%D0%BD%D0%B0%20"
        "Alibaba%20Ascend%3A%20%D0%90%D0%BB%D0%B5%D0%BA%D1%81%D0%B0%D0%BD%D0%B4%D1%80%20"
        "%D0%9C%D1%8F%D1%81%D0%BD%D0%B8%D0%BA%D0%BE%D0%B2"
    )
    assert keyboard.inline_keyboard[1][0].callback_data == (
        "admin:ascend:grant:request-123"
    )
    assert keyboard.inline_keyboard[1][1].callback_data == (
        "admin:reject:request-123"
    )


def test_mlsub_always_requires_separate_admin_approval() -> None:
    application = application_from_request(
        Profile(101, "alice_tg", "Alice Smith", "Bob Jones"),
        ServerRequest(
            telegram_user_id=101,
            telegram_username="alice_tg",
            server_username="alice",
            access_term="бессрочно",
            activity="distributed training",
            servers=("mlsub",),
        ),
    )

    assert request_requires_admin(application, user_is_known=True)
    assert not request_requires_admin(
        application, user_is_known=True, mlsub_is_missing=False
    )


def test_ascend_always_requires_manual_admin_flow() -> None:
    application = application_from_request(
        Profile(101, "alice_tg", "Alice Smith", "Bob Jones"),
        ServerRequest(
            101,
            "alice_tg",
            "alice",
            "бессрочно",
            "Ascend experiments",
            ("alibaba_ascend",),
        ),
    )

    assert request_requires_admin(application, user_is_known=True)


def test_known_user_can_still_add_managed_server_without_second_approval() -> None:
    application = application_from_request(
        Profile(101, "alice_tg", "Alice Smith", "Bob Jones"),
        ServerRequest(
            telegram_user_id=101,
            telegram_username="alice_tg",
            server_username="alice",
            access_term="бессрочно",
            activity="fine-tuning",
            servers=("brain_lab",),
        ),
    )

    assert not request_requires_admin(application, user_is_known=True)
    assert request_requires_admin(application, user_is_known=False)


def test_gpu_process_buttons_are_bound_to_one_action_token() -> None:
    confirmation = gpu_stop_confirmation_keyboard("safe-token")
    force = gpu_force_keyboard("safe-token")

    assert confirmation.inline_keyboard[0][0].callback_data == (
        "gpu:term:safe-token"
    )
    assert confirmation.inline_keyboard[0][1].callback_data == (
        "gpu:cancel:safe-token"
    )
    assert force.inline_keyboard[0][0].callback_data == "gpu:kill:safe-token"
    assert force.inline_keyboard[0][1].callback_data == "gpu:cancel:safe-token"


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
        ["🖥 Все серверы лаборатории", "➕ Получить доступ"],
        ["📚 Сайт и правила", "📡 Статус серверов"],
    ]
    access_button = MAIN_MENU.keyboard[1][1]
    assert access_button.web_app is not None
    assert access_button.web_app.url == (
        "https://vepricov.github.io/ssh_websile/server-form.html"
    )


def test_main_menu_personalizes_access_form_from_saved_account() -> None:
    menu = main_menu(
        server_username="alice",
        existing_services=("brain_lab", "shkodnik_opt"),
    )

    access_button = menu.keyboard[1][1]
    assert access_button.web_app is not None
    assert access_button.web_app.url.endswith(
        "?username=alice&existing=brain_lab%2Cshkodnik_opt"
    )


def test_registry_link_requires_one_tap_confirmation() -> None:
    match = registry_match()

    text = registry_confirmation_text(match)
    keyboard = registry_confirmation_keyboard("link-123")

    assert "Alice Smith" in text
    assert "Новый сервер (BrainLab)" in text
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


def test_legacy_registry_link_can_be_claimed_by_current_username_holder() -> None:
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

    assert registry_link_can_be_claimed(legacy, 101)
    assert registry_link_can_be_claimed(trusted, 101)
    assert not registry_link_can_be_claimed(trusted, 202)


def test_my_info_combines_profile_and_registered_services() -> None:
    profile = Profile(101, "alice_tg", "Alice <Smith>", "Bob Jones")

    text = my_info_text(profile, registry_match(), [])

    assert "Alice &lt;Smith&gt;" in text
    assert "<b>Мой профиль</b>" in text
    assert "@alice_tg" in text
    assert "Ник на серверах: <code>alice</code>" in text
    assert "Новый сервер (BrainLab)" in text
    assert "Яндекс-сервер" in text
    assert "Пароли к старым доступам" in text
    assert "@shkodnik1917" in text


def test_available_server_message_is_complete() -> None:
    catalog = available_servers_text()
    keyboard = available_servers_keyboard()

    assert "Старый сервер (MIPT)" in catalog
    assert "Новый сервер (BrainLab)" in catalog
    assert "H200 (VV)" in catalog
    assert "@undef1ne5" in catalog
    assert "helpdesk02@vkusvill.ru" in catalog
    assert "Получить доступ" in catalog
    assert "@shkodnik1917" not in catalog
    assert "mlsub / Cloud.ru" in catalog
    assert "отдельный вычислительный сервис" in catalog
    assert "уже выданный доступ к BrainLab" in catalog
    assert "отдельного одобрения администратора" in catalog
    assert "Alibaba Ascend" in catalog
    assert "ручную выдачу" in catalog
    assert "@fckand1" in catalog
    assert '<a href="https://t.me/aloeme">@aloeme</a>' in catalog
    assert BOT_DEFAULTS.link_preview_is_disabled is True
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "☁️ mlsub / Cloud.ru" in labels
    assert "🧠 Alibaba Ascend" in labels


def test_onboarding_explains_the_main_user_flow() -> None:
    text = onboarding_text()

    assert "Мой профиль" in text
    assert "Все серверы лаборатории" in text
    assert "Получить доступ" in text
    assert "Мои серверы" in text
    assert "пароли" in text


def test_demo_accesses_are_fake_and_do_not_auto_add_brain_lab() -> None:
    accesses = demo_accesses("alice", ("mlsub", "vv_h200"))

    assert [access.host for access in accesses] == ["mlsub", "vv_h200"]
    assert accesses[0].password is None
    assert accesses[1].password == "DEMO-PASSWORD"
    assert all(access.created is False for access in accesses)
