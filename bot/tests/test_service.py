import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from server_access_bot.domain import Application, Profile, ProvisionedAccess
from server_access_bot.service import AccessService, ValidationError, validate
from server_access_bot.storage import Storage


class FakeProvisioner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.unavailable: set[tuple[str, str]] = set()

    def username_available(self, host: str, username: str) -> bool:
        return (host, username) not in self.unavailable

    def provision(self, host: str, username: str) -> ProvisionedAccess:
        self.calls.append((host, username))
        return ProvisionedAccess(
            host=host,
            username=username,
            password="generated-secret",
            connection="ssh alice@example.org",
            group_url="https://t.me/example",
            created=True,
        )


def test_profile_is_saved_without_server_fields(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    profile = Profile(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        recommender="Bob Jones",
    )

    storage.save_profile(profile)

    assert storage.profile_for(101) == profile
    assert storage.server_username_for(101) is None


def test_profile_storage_is_added_to_an_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "bot.sqlite3"
    Storage(database, b"0" * 32)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO users (
                telegram_user_id, telegram_username, full_name, server_username,
                access_term, recommender, activity
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                "alice_tg",
                "Alice Smith",
                "alice",
                "бессрочно",
                "Bob Jones",
                "fine-tuning",
            ),
        )
        connection.execute("DROP TABLE profiles")

    storage = Storage(database, b"0" * 32)

    assert storage.profile_for(101) == Profile(
        101, "alice_tg", "Alice Smith", "Bob Jones"
    )


def test_access_term_accepts_only_indefinite_or_current_iso_date() -> None:
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="fine-tuning",
        servers=("brain_lab",),
    )

    validate(application)
    validate(replace(application, access_term=today.isoformat()))
    validate(replace(application, access_term=(today + timedelta(days=1)).isoformat()))

    invalid_terms = (
        "tomorrow",
        (today - timedelta(days=1)).isoformat(),
    )
    for access_term in invalid_terms:
        try:
            validate(replace(application, access_term=access_term))
        except ValidationError as error:
            assert "срок" in str(error).lower()
        else:
            raise AssertionError(f"invalid access term was accepted: {access_term}")


def test_user_can_provision_and_read_own_credentials(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    provisioner = FakeProvisioner()
    service = AccessService(storage, provisioner)
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="fine-tuning",
        servers=("brain_lab",),
    )

    result = service.submit(application)

    assert result[0].password == "generated-secret"
    assert provisioner.calls == [("brain_lab", "alice")]
    assert service.credentials_for(101)[0].password == "generated-secret"
    assert service.credentials_for(202) == []
    assert b"generated-secret" not in (tmp_path / "bot.sqlite3").read_bytes()


def test_invalid_unix_username_is_rejected_before_provisioning(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    provisioner = FakeProvisioner()
    service = AccessService(storage, provisioner)
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="Alice; rm -rf",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="fine-tuning",
        servers=("brain_lab",),
    )

    try:
        service.submit(application)
    except ValidationError as error:
        assert "ник" in str(error)
    else:
        raise AssertionError("invalid application was accepted")

    assert provisioner.calls == []


def test_repeat_request_only_provisions_new_servers(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    provisioner = FakeProvisioner()
    service = AccessService(storage, provisioner)
    base = {
        "telegram_user_id": 101,
        "telegram_username": "alice_tg",
        "full_name": "Alice Smith",
        "server_username": "alice",
        "access_term": "бессрочно",
        "recommender": "Bob Jones",
        "activity": "fine-tuning",
    }

    service.submit(Application(servers=("brain_lab",), **base))
    result = service.submit(Application(servers=("brain_lab", "shkodnik_opt"), **base))

    assert [access.host for access in result] == ["shkodnik_opt"]
    assert provisioner.calls == [
        ("brain_lab", "alice"),
        ("shkodnik_opt", "alice"),
    ]


def test_existing_user_cannot_switch_unix_username(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    service = AccessService(storage, FakeProvisioner())
    base = {
        "telegram_user_id": 101,
        "telegram_username": "alice_tg",
        "full_name": "Alice Smith",
        "access_term": "бессрочно",
        "recommender": "Bob Jones",
        "activity": "fine-tuning",
        "servers": ("brain_lab",),
    }
    service.submit(Application(server_username="alice", **base))

    try:
        service.submit(Application(server_username="alice2", **base))
    except ValidationError as error:
        assert "alice" in str(error)
    else:
        raise AssertionError("server username was changed")


def test_existing_unowned_username_is_rejected_before_any_creation(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    provisioner = FakeProvisioner()
    provisioner.unavailable.add(("brain_lab", "alice"))
    service = AccessService(storage, provisioner)
    application = Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="fine-tuning",
        servers=("brain_lab", "shkodnik_opt"),
    )

    try:
        service.submit(application)
    except ValidationError as error:
        assert "brain_lab" in str(error)
    else:
        raise AssertionError("existing username was accepted")

    assert provisioner.calls == []
    assert storage.server_username_for(101) is None
