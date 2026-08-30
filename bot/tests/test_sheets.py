from datetime import UTC, datetime

from server_access_bot.domain import Application
from server_access_bot.sheets import application_values


def test_application_maps_to_existing_form_columns_plus_bot_metadata() -> None:
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

    values = application_values(
        application,
        status="created: brain_lab, vv_h200",
        now=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
    )

    assert values == [
        "30-08-2026 12:00:00 UTC",
        "Alice Smith",
        "alice",
        "бессрочно",
        "Bob Jones",
        "@alice_tg",
        "Новый (BrainLab) -- x4 A100 80гб, H200 (VB) - x8 H200 140гб",
        "fine-tuning",
        "101",
        "created: brain_lab, vv_h200",
        "30-08-2026 12:00:00 UTC",
    ]
