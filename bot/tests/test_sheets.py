from datetime import UTC, datetime

import pytest

from server_access_bot.domain import Application, Profile
from server_access_bot.sheets import (
    BOT_HEADERS,
    BOT_REQUEST_HEADERS,
    BOT_USER_HEADERS,
    FORM_HEADERS,
    RegistryAmbiguousError,
    bot_registry_match_from_rows,
    bot_request_values,
    bot_user_values,
    pad_snapshot_values,
    registry_claim_plan_from_sheet_values,
    registry_match_by_server_username_from_sheet_values,
    registry_match_from_rows,
    registry_match_from_sheet_values,
)


def registry_row(
    full_name: str,
    server_username: str,
    telegram_username: str,
    servers: str,
    recommender: str = "Bob Jones",
    access_term: str = "бессрочно",
    activity: str = "fine-tuning",
    telegram_user_id: str = "",
) -> list[str]:
    return [
        "timestamp",
        full_name,
        server_username,
        access_term,
        recommender,
        telegram_username,
        servers,
        activity,
        telegram_user_id,
    ]


def test_application_maps_to_bot_user_registry() -> None:
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="fine-tuning",
        servers=("brain_lab", "vv_h200"),
    )

    values = bot_user_values(
        application,
        status="Доступ выдан",
        registry_servers=("brain_lab", "vv_h200"),
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert values == [
        "101",
        "@alice_tg",
        "Alice Smith",
        "alice",
        "бессрочно",
        "Bob Jones",
        "fine-tuning",
        "Новый сервер (BrainLab); H200",
        "Доступ выдан",
        "30-08-2026 12:00:00 UTC",
    ]


def test_profile_maps_to_a_bot_registry_row_without_server_data() -> None:
    profile = Profile(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        recommender="Bob Jones",
    )

    values = bot_user_values(
        profile,
        status="Профиль заполнен",
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert values == [
        "101",
        "@alice_tg",
        "Alice Smith",
        "",
        "",
        "Bob Jones",
        "",
        "",
        "Профиль заполнен",
        "30-08-2026 12:00:00 UTC",
    ]


def test_bot_registry_is_authoritative_for_granted_servers() -> None:
    rows = [
        list(BOT_USER_HEADERS),
        [
            "101", "@alice_tg", "Alice Smith", "alice", "бессрочно",
            "Bob Jones", "fine-tuning", "Новый сервер (BrainLab); H200",
            "Доступ выдан", "30-08-2026 12:00:00 UTC",
        ],
    ]

    match = bot_registry_match_from_rows(rows, telegram_username="ALICE_TG")

    assert match is not None
    assert match.telegram_user_id == 101
    assert match.servers == ("brain_lab", "vv_h200")


@pytest.mark.parametrize("label", ["Alibaba", "Alibaba Ascend"])
def test_bot_registry_accepts_current_and_legacy_alibaba_labels(label) -> None:
    rows = [
        list(BOT_USER_HEADERS),
        [
            "101", "@alice_tg", "Alice Smith", "alice", "бессрочно",
            "Bob Jones", "NPU experiments", label, "Доступ выдан",
            "30-08-2026 12:00:00 UTC",
        ],
    ]

    match = bot_registry_match_from_rows(rows, telegram_username="alice_tg")

    assert match is not None
    assert match.servers == ("alibaba_ascend",)


@pytest.mark.parametrize("label", ["Alibaba", "Alibaba Ascend"])
def test_legacy_registry_accepts_current_and_legacy_alibaba_labels(label) -> None:
    rows = [list(FORM_HEADERS), registry_row("Alice Smith", "alice", "alice_tg", label)]

    match = registry_match_from_sheet_values(rows, "alice_tg")

    assert match is not None
    assert match.servers == ("alibaba_ascend",)


def test_bot_registry_rejects_duplicate_telegram_identity() -> None:
    rows = [
        list(BOT_USER_HEADERS),
        ["101", "@alice_tg"],
        ["202", "@alice_tg"],
    ]

    with pytest.raises(RegistryAmbiguousError):
        bot_registry_match_from_rows(rows, telegram_username="alice_tg")


def test_profile_only_bot_row_is_not_treated_as_granted_registry_access() -> None:
    rows = [
        list(BOT_USER_HEADERS),
        [
            "101", "@alice_tg", "Alice Smith", "", "", "Bob Jones", "",
            "", "Профиль заполнен", "30-08-2026 12:00:00 UTC",
        ],
    ]

    assert bot_registry_match_from_rows(rows, telegram_username="alice_tg") is None


def test_failed_application_without_access_does_not_mask_legacy_registry() -> None:
    rows = [
        list(BOT_USER_HEADERS),
        [
            "101", "@alice_tg", "Alice Smith", "alice", "бессрочно",
            "Bob Jones", "fine-tuning", "", "Ошибка выдачи",
            "30-08-2026 12:00:00 UTC",
        ],
    ]

    assert bot_registry_match_from_rows(rows, telegram_username="alice_tg") is None


def test_mlsub_has_a_distinct_sheet_label() -> None:
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="distributed training",
        servers=("mlsub",),
    )

    values = bot_user_values(application, "Доступ выдан", ("mlsub",))

    assert values[7] == "mlsub / Cloud.ru"


def test_ascend_has_a_distinct_sheet_label() -> None:
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="Alibaba NPU experiments",
        servers=("alibaba_ascend",),
    )

    values = bot_user_values(application, "Доступ выдан", ("alibaba_ascend",))

    assert values[7] == "Alibaba"


def test_each_request_has_a_separate_status_row() -> None:
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="Alibaba NPU experiments",
        servers=("alibaba_ascend",),
    )

    values = bot_request_values(
        "request-1",
        application,
        "Ожидает одобрения",
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert len(values) == len(BOT_REQUEST_HEADERS)
    assert values[0] == "request-1"
    assert values[5] == "Alibaba"
    assert values[6] == "Ожидает одобрения"


def test_shorter_snapshot_clears_stale_tail_rows() -> None:
    values = [["header"], ["current-user"]]

    padded = pad_snapshot_values(values, previous_row_count=4, column_count=3)

    assert padded == [
        ["header"],
        ["current-user"],
        ["", "", ""],
        ["", "", ""],
    ]


def test_registry_rows_for_same_identity_are_merged_by_server() -> None:
    rows = [
        registry_row(
            "Alice Smith",
            "alice",
            "@Alice_TG",
            "Старый сервер (MIPT) -- x4 RTX 2080 11гб, x2 A100 40гб, x2 A100 80гб",
        ),
        registry_row(
            "Alice Smith",
            "alice",
            "https://t.me/alice_tg/",
            "Новый сервер (BrainLab) -- x4 A100 80гб, H200 (VB) - x8 H200 140гб",
            access_term="2027-01-01",
        ),
    ]

    match = registry_match_from_rows(rows, "alice_tg")

    assert match is not None
    assert match.full_name == "Alice Smith"
    assert match.server_username == "alice"
    assert match.access_term == "2027-01-01"
    assert match.servers == ("shkodnik_opt", "brain_lab", "vv_h200")


def test_registry_owner_is_found_by_unix_username_across_all_servers() -> None:
    rows = [
        list(FORM_HEADERS) + [BOT_HEADERS[0]],
        registry_row(
            "Peter Badolya",
            "akzium",
            "@Postscripter",
            "Старый (mipt)",
            telegram_user_id="4242",
        ),
    ]

    match = registry_match_by_server_username_from_sheet_values(rows, "AKZIUM")

    assert match is not None
    assert match.telegram_username == "postscripter"
    assert match.telegram_user_id == 4242
    assert match.server_username == "akzium"


def test_registry_owner_without_bot_owned_id_stays_untrusted() -> None:
    rows = [
        list(FORM_HEADERS),
        registry_row(
            "Peter Badolya", "akzium", "@Postscripter", "Старый (mipt)"
        ),
    ]

    match = registry_match_by_server_username_from_sheet_values(rows, "akzium")

    assert match is not None
    assert match.telegram_username == "postscripter"
    assert match.telegram_user_id is None


def test_registry_ignores_legacy_name_order_and_invalid_unix_name() -> None:
    rows = [
        registry_row("Иван Голдов", "igoldov", "@qclsr", "H200 (VB)"),
        registry_row(
            "Голдов Иван",
            "Иван Голдов",
            "qclsr",
            "Новый (BrainLab)",
        ),
    ]

    match = registry_match_from_rows(rows, "qclsr")

    assert match is not None
    assert match.full_name == "Голдов Иван"
    assert match.server_username == "igoldov"
    assert match.servers == ("brain_lab", "vv_h200")


def test_registry_imports_profile_when_legacy_unix_name_is_missing() -> None:
    match = registry_match_from_rows(
        [
            registry_row(
                "Игорь Шалыгин",
                "Игорь Шалыгин",
                "@igor_vsh",
                "Новый (BrainLab)",
            )
        ],
        "igor_vsh",
    )

    assert match is not None
    assert match.full_name == "Игорь Шалыгин"
    assert match.server_username == ""
    assert match.servers == ("brain_lab",)


def test_registry_yandex_service_is_imported_but_not_managed() -> None:
    match = registry_match_from_rows(
        [
            registry_row(
                "Alice Smith",
                "alice",
                "@alice_tg",
                "Яндекс - x8 A100 80гб. Тут надо в тг скинуть @aloeme публичный ключ",
            )
        ],
        "@ALICE_TG",
    )

    assert match is not None
    assert match.servers == ("yandex",)


def test_registry_match_carries_bot_owned_numeric_telegram_id() -> None:
    match = registry_match_from_rows(
        [
            registry_row(
                "Alice Smith",
                "alice",
                "@alice_tg",
                "Новый (BrainLab)",
                telegram_user_id="101",
            )
        ],
        "alice_tg",
        trust_telegram_user_id=True,
    )

    assert match is not None
    assert match.telegram_user_id == 101


def test_legacy_registry_match_has_no_trusted_numeric_telegram_id() -> None:
    match = registry_match_from_rows(
        [registry_row("Alice Smith", "alice", "@alice_tg", "Новый (BrainLab)")],
        "alice_tg",
    )

    assert match is not None
    assert match.telegram_user_id is None


def test_legacy_registry_claim_updates_all_matching_rows_with_numeric_id() -> None:
    rows = [
        list(FORM_HEADERS),
        registry_row("Амир Зайнуллин", "amir_iny", "@amir_iny", "H200 (VB)"),
        registry_row(
            "Амир Зайнуллин",
            "amir_iny",
            "amir_iny",
            "Новый (BrainLab)",
        ),
    ]

    match, row_numbers = registry_claim_plan_from_sheet_values(
        rows, "amir_iny", 101
    )

    assert match is not None
    assert match.telegram_user_id == 101
    assert match.servers == ("brain_lab", "vv_h200")
    assert row_numbers == (2, 3)


def test_sheet_schema_controls_whether_numeric_id_is_trusted() -> None:
    data_row = registry_row(
        "Alice Smith",
        "alice",
        "@alice_tg",
        "Новый (BrainLab)",
        telegram_user_id="101",
    )
    trusted = registry_match_from_sheet_values(
        [list(FORM_HEADERS) + [BOT_HEADERS[0]], data_row], "alice_tg"
    )
    missing_header = registry_match_from_sheet_values(
        [list(FORM_HEADERS), data_row], "alice_tg"
    )
    wrong_header = registry_match_from_sheet_values(
        [list(FORM_HEADERS) + ["Legacy numeric field"], data_row], "alice_tg"
    )

    assert trusted is not None and trusted.telegram_user_id == 101
    assert missing_header is not None and missing_header.telegram_user_id is None
    assert wrong_header is not None and wrong_header.telegram_user_id is None


@pytest.mark.parametrize(
    "telegram_user_ids",
    [("not-a-number",), ("101", "202")],
)
def test_invalid_or_conflicting_trusted_ids_fail_closed(
    telegram_user_ids: tuple[str, ...],
) -> None:
    rows = [
        registry_row(
            "Alice Smith",
            "alice",
            "@alice_tg",
            "Новый (BrainLab)",
            telegram_user_id=telegram_user_id,
        )
        for telegram_user_id in telegram_user_ids
    ]

    with pytest.raises(RegistryAmbiguousError):
        registry_match_from_rows(rows, "alice_tg", trust_telegram_user_id=True)


def test_conflicting_registry_rows_are_not_silently_linked() -> None:
    rows = [
        registry_row("Alice Smith", "alice", "@alice_tg", "Новый (BrainLab)"),
        registry_row("Alicia Smith", "alicia", "@alice_tg", "H200 (VB)"),
    ]

    with pytest.raises(RegistryAmbiguousError):
        registry_match_from_rows(rows, "alice_tg")
