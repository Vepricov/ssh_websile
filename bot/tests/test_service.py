import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from server_access_bot.domain import (
    Application,
    Profile,
    ProvisionedAccess,
    RegistryMatch,
)
from server_access_bot.service import AccessService, ValidationError, validate
from server_access_bot.storage import RegistryAlreadyClaimedError, Storage


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


def test_confirmed_registry_match_populates_profile_and_services(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    match = RegistryMatch(
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

    storage.save_registry_match(101, "alice_tg", match)

    assert storage.profile_for(101) == Profile(
        101, "alice_tg", "Alice Smith", "Bob Jones"
    )
    assert storage.registry_match_for(101) == match
    assert storage.registry_services_for(101) == ("brain_lab", "yandex")
    assert storage.server_username_for(101) == "alice"


def test_registry_identity_can_only_be_claimed_by_one_telegram_id(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    match = RegistryMatch(
        telegram_username="alice_tg",
        full_name="Alice Smith",
        server_username="alice",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="fine-tuning",
        servers=("brain_lab",),
        source_row_count=1,
        telegram_user_id=101,
    )
    storage.save_registry_match(101, "alice_tg", match)

    try:
        storage.save_registry_match(202, "alice_tg", match)
    except RegistryAlreadyClaimedError:
        pass
    else:
        raise AssertionError("registry identity was claimed by a second Telegram ID")

    assert storage.profile_for(202) is None
    assert storage.registry_match_for(202) is None


def test_legacy_registry_identity_can_be_claimed_once_by_current_username(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    legacy = RegistryMatch(
        telegram_username="amir_iny",
        full_name="Амир Зайнуллин",
        server_username="amir_iny",
        access_term="бессрочно",
        recommender="Andrey",
        activity="research",
        servers=("brain_lab", "vv_h200"),
        source_row_count=2,
    )

    storage.save_registry_match(
        101,
        "amir_iny",
        legacy,
        allow_legacy_claim=True,
    )

    imported = storage.registry_match_for(101)
    assert imported is not None
    assert imported.telegram_user_id == 101
    assert imported.servers == ("brain_lab", "vv_h200")
    try:
        storage.save_registry_match(
            202,
            "amir_iny",
            legacy,
            allow_legacy_claim=True,
        )
    except RegistryAlreadyClaimedError:
        pass
    else:
        raise AssertionError("legacy registry identity was claimed twice")


def test_registry_link_preserves_an_existing_edited_profile(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    storage.save_profile(Profile(101, "alice_tg", "Alice Corrected", "@new_ref"))

    storage.save_registry_match(
        101,
        "alice_tg",
        RegistryMatch(
            telegram_username="alice_tg",
            full_name="Alice Old Name",
            server_username="alice",
            access_term="бессрочно",
            recommender="Bob Jones",
            activity="fine-tuning",
            servers=("brain_lab",),
            source_row_count=1,
            telegram_user_id=101,
        ),
    )

    assert storage.profile_for(101) == Profile(
        101, "alice_tg", "Alice Corrected", "@new_ref"
    )
    assert storage.registry_services_for(101) == ("brain_lab",)
    provisioner = FakeProvisioner()
    service = AccessService(storage, provisioner)
    assert (
        service.submit(
            Application(
                telegram_user_id=101,
                telegram_username="alice_tg",
                full_name="Alice Corrected",
                server_username="alice",
                access_term="бессрочно",
                recommender="@new_ref",
                activity="fine-tuning",
                servers=("brain_lab",),
            )
        )
        == []
    )
    assert provisioner.calls == []


def test_registry_service_is_not_provisioned_again(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    storage.save_registry_match(
        101,
        "alice_tg",
        RegistryMatch(
            telegram_username="alice_tg",
            full_name="Alice Smith",
            server_username="alice",
            access_term="бессрочно",
            recommender="Bob Jones",
            activity="fine-tuning",
            servers=("brain_lab",),
            source_row_count=1,
            telegram_user_id=101,
        ),
    )
    provisioner = FakeProvisioner()
    service = AccessService(storage, provisioner)

    result = service.submit(
        Application(
            telegram_user_id=101,
            telegram_username="alice_tg",
            full_name="Alice Smith",
            server_username="alice",
            access_term="бессрочно",
            recommender="Bob Jones",
            activity="fine-tuning",
            servers=("brain_lab",),
        )
    )

    assert result == []
    assert provisioner.calls == []


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


def test_imported_profile_can_store_encrypted_per_server_credentials(
    tmp_path: Path,
) -> None:
    database = tmp_path / "bot.sqlite3"
    storage = Storage(database, b"0" * 32)
    storage.save_profile(Profile(101, "alice_tg", "Alice Smith", "Bob Jones"))

    storage.save_access(
        101,
        ProvisionedAccess(
            host="shkodnik_opt",
            username="alice-old",
            password="legacy-secret",
            connection="ssh old",
            group_url="",
            created=False,
        ),
    )

    [access] = storage.credentials_for(101)
    assert access.username == "alice-old"
    assert access.password == "legacy-secret"
    assert b"legacy-secret" not in database.read_bytes()


def test_pending_application_can_only_be_claimed_once(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
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

    storage.save_pending_request("request-123", application)

    assert storage.claim_pending_request("request-123") == application
    assert storage.claim_pending_request("request-123") is None
    storage.resolve_pending_request("request-123", "approved")


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


def test_mlsub_is_a_separate_service_but_ensures_brain_lab_access(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"0" * 32)
    provisioner = FakeProvisioner()
    service = AccessService(storage, provisioner)

    result = service.submit(
        Application(
            telegram_user_id=101,
            telegram_username="alice_tg",
            full_name="Alice Smith",
            server_username="alice",
            access_term="бессрочно",
            recommender="Bob Jones",
            activity="distributed training",
            servers=("mlsub",),
        )
    )

    assert [access.host for access in result] == ["brain_lab", "mlsub"]
    assert provisioner.calls == [("brain_lab", "alice"), ("mlsub", "alice")]


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
