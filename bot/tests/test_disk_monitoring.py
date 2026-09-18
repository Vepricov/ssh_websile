import asyncio
import json

from server_access_bot.disk_monitoring import (
    DiskAlert,
    DiskCleanupAction,
    DiskCleanupResult,
    DiskDirectoryUsage,
    DiskFilesystemStatus,
    DiskHostStatus,
    DiskMonitor,
    DiskScanner,
    deliver_disk_alerts,
    deliver_disk_cleanup_requests,
    format_disk_status,
)
from server_access_bot.domain import (
    Application,
    DiskUsageSample,
    Profile,
    ProvisionedAccess,
    RegistryMatch,
    ServerChatBinding,
)
from server_access_bot.storage import Storage


def test_server_chat_binding_keeps_forum_topic_and_can_be_rebound(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)

    storage.bind_server_chat("brain_lab", -100123, 17, 101)
    assert storage.server_chat_binding_for("brain_lab") == ServerChatBinding(
        host="brain_lab",
        chat_id=-100123,
        message_thread_id=17,
        bound_by=101,
    )

    storage.bind_server_chat("brain_lab", -100456, None, 101)
    assert storage.server_chat_binding_for("brain_lab") == ServerChatBinding(
        host="brain_lab",
        chat_id=-100456,
        message_thread_id=None,
        bound_by=101,
    )


def test_disk_scanner_parses_filesystems_and_marks_unreachable_host() -> None:
    report = json.dumps(
        {
            "filesystems": [
                {
                    "key": "dev:42",
                    "paths": ["/", "/home"],
                    "total_bytes": 1000,
                    "used_bytes": 900,
                    "available_bytes": 100,
                    "total_inodes": 200,
                    "free_inodes": 50,
                }
            ]
        }
    )

    def runner(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        assert command == DiskScanner.SUMMARY_QUERY
        return (0, report) if host == "brain_lab" else (255, "")

    statuses = DiskScanner(
        hosts=("brain_lab", "vv_h200"), runner=runner
    ).scan()

    assert statuses[0].reachable is True
    assert statuses[0].filesystems[0].paths == ("/", "/home")
    assert statuses[0].filesystems[0].used_bytes == 900
    assert statuses[1].host == "vv_h200"
    assert statuses[1].reachable is False


def test_disk_scanner_keeps_top_ten_home_directories() -> None:
    report = json.dumps(
        {
            "directories": [
                {"path": f"/home/user{index}", "size_bytes": 20 - index}
                for index in range(12)
            ]
        }
    )

    scanner = DiskScanner(("brain_lab",), runner=lambda _host, _command: (0, report))

    assert len(scanner.top_home("brain_lab")) == 10
    assert scanner.top_home("brain_lab")[-1].path == "/home/user9"


def test_disk_scanner_parses_allowlisted_cleanup_report() -> None:
    report = json.dumps(
        {
            "before_available_bytes": 10,
            "after_available_bytes": 18,
            "actions": [
                {"name": "Кеш apt", "freed_bytes": 8, "status": "ok"}
            ],
        }
    )

    def runner(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        assert host == "brain_lab"
        assert command == DiskScanner.CLEANUP_ROOT_QUERY
        return 0, report

    result = DiskScanner(("brain_lab",), runner=runner).cleanup_root("brain_lab")

    assert result == DiskCleanupResult(
        before_available_bytes=10,
        after_available_bytes=18,
        actions=(DiskCleanupAction("Кеш apt", 8),),
    )


def test_disk_monitor_alerts_on_warning_and_escalates_to_critical(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)

    class FakeScanner:
        used_bytes = 800

        def scan(self):
            return (
                DiskHostStatus(
                    host="brain_lab",
                    reachable=True,
                    filesystems=(
                        DiskFilesystemStatus(
                            key="dev:42",
                            paths=("/", "/home"),
                            total_bytes=1000,
                            used_bytes=self.used_bytes,
                            available_bytes=1000 - self.used_bytes,
                            total_inodes=1000,
                            free_inodes=500,
                        ),
                    ),
                ),
            )

    scanner = FakeScanner()
    monitor = DiskMonitor(storage, scanner, notification_cooldown_seconds=21600)

    assert monitor.check(now=0) == ()

    scanner.used_bytes = 900
    warning = monitor.check(now=1800)
    assert len(warning) == 1
    assert warning[0].level == "warning"
    assert warning[0].growth_since_previous_bytes == 100

    scanner.used_bytes = 910
    assert monitor.check(now=3600) == ()

    scanner.used_bytes = 960
    critical = monitor.check(now=5400)
    assert len(critical) == 1
    assert critical[0].level == "critical"


def test_disk_alert_is_sent_to_admin_with_growth_top_and_cleanup(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    gib = 1024**3
    alert = DiskAlert(
        level="critical",
        sample=DiskUsageSample(
            host="brain_lab",
            filesystem_key="dev:42",
            paths=("/", "/home"),
            total_bytes=1000 * gib,
            used_bytes=900 * gib,
            available_bytes=100 * gib,
            total_inodes=1000,
            free_inodes=200,
            sampled_at=100,
        ),
        growth_since_previous_bytes=12 * gib,
        growth_since_day_bytes=85 * gib,
        day_baseline_at=100 - 86400,
    )

    class FakeScanner:
        def top_home(self, host):
            assert host == "brain_lab"
            return (
                DiskDirectoryUsage("/home/alice", 400 * gib),
                DiskDirectoryUsage("/home/bob", 250 * gib),
            )

        def cleanup_root(self, host):
            assert host == "brain_lab"
            return DiskCleanupResult(
                before_available_bytes=10 * gib,
                after_available_bytes=18 * gib,
                actions=(DiskCleanupAction("Системные журналы", 8 * gib),),
            )

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    delivered = asyncio.run(
        deliver_disk_alerts(
            bot,
            storage,
            FakeScanner(),
            (alert,),
            admin_user_ids=frozenset({999}),
        )
    )

    assert delivered == ("brain_lab",)
    assert bot.messages[0][0] == 999
    assert "message_thread_id" not in bot.messages[0][2]
    text = bot.messages[0][1]
    assert "Новый сервер (BrainLab)" in text
    assert "/ и /home" in text
    assert "Свободно: <b>100.0 GB</b>" in text
    assert "С прошлого замера: +12.0 GB" in text
    assert "За 24 часа: +85.0 GB" in text
    assert "<code>/home/alice</code> · 400.0 GB" in text
    assert "До очистки: 10.0 GB" in text
    assert "После очистки: <b>18.0 GB</b>" in text
    assert "Системные журналы: −8.0 GB" in text


def test_warning_disk_alert_is_not_sent_to_admin(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    gib = 1024**3
    alert = DiskAlert(
        level="warning",
        sample=DiskUsageSample(
            "brain_lab", "dev:42", ("/home",), 1000 * gib, 850 * gib,
            150 * gib, 1000, 900, 100,
        ),
        growth_since_previous_bytes=0,
        growth_since_day_bytes=0,
        day_baseline_at=0,
    )

    class FakeScanner:
        def top_home(self, _host):
            return ()

        def cleanup_root(self, _host):
            return None

    class FakeBot:
        messages = []

        async def send_message(self, *args, **kwargs):
            self.messages.append((args, kwargs))

    bot = FakeBot()
    delivered = asyncio.run(
        deliver_disk_alerts(
            bot,
            storage,
            FakeScanner(),
            (alert,),
            admin_user_ids=frozenset({999}),
        )
    )

    assert delivered == ()
    assert bot.messages == []


def test_large_home_users_are_notified_or_sent_to_admin_for_forwarding(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    gib = 1024**3
    storage.save_application(
        Application(
            telegram_user_id=101,
            telegram_username="alice_tg",
            full_name="Alice Example",
            server_username="alice",
            access_term="бессрочно",
            recommender="Andrey Example",
            activity="Research",
            servers=("brain_lab",),
        )
    )
    storage.save_access(
        101,
        ProvisionedAccess(
            host="brain_lab",
            username="alice",
            password="secret",
            connection="ssh alice@example",
            group_url="",
            created=True,
        ),
    )
    storage.record_disk_sample(
        DiskUsageSample(
            host="brain_lab",
            filesystem_key="dev:42",
            paths=("/home",),
            total_bytes=10_000 * gib,
            used_bytes=9_600 * gib,
            available_bytes=400 * gib,
            total_inodes=1000,
            free_inodes=900,
            sampled_at=100,
        )
    )

    class FakeScanner:
        hosts = ("brain_lab",)

        def top_home(self, host):
            assert host == "brain_lab"
            return (
                DiskDirectoryUsage("/home/alice", 700 * gib),
                DiskDirectoryUsage("/home/bob", 600 * gib),
                DiskDirectoryUsage("/home/small", 399 * gib),
            )

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))

    def registry_lookup(username):
        assert username == "bob"
        return RegistryMatch(
            telegram_username="bob_tg",
            full_name="Bob Example",
            server_username="bob",
            access_term="бессрочно",
            recommender="Andrey Example",
            activity="Research",
            servers=("brain_lab",),
            source_row_count=1,
        )

    bot = FakeBot()
    delivered = asyncio.run(
        deliver_disk_cleanup_requests(
            bot,
            storage,
            FakeScanner(),
            admin_user_ids=frozenset({999}),
            now=200,
            registry_lookup=registry_lookup,
        )
    )

    assert delivered == (("brain_lab", "alice"), ("brain_lab", "bob"))
    user_message = next(item for item in bot.messages if item[0] == 101)
    assert "700.0 GB" in user_message[1]
    assert "осталось 4.0%" in user_message[1]
    assert user_message[2]["protect_content"] is True
    assert (
        user_message[2]["reply_markup"].inline_keyboard[0][0].callback_data
        .startswith("disk:cleaned:")
    )
    admin_message = next(item for item in bot.messages if item[0] == 999)
    assert "Telegram: @bob_tg" in admin_message[1]
    assert "Готовое сообщение" in admin_message[1]
    assert "/home/bob" in admin_message[1]
    assert all("small" not in item[1] for item in bot.messages)

    bot.messages.clear()
    assert (
        asyncio.run(
            deliver_disk_cleanup_requests(
                bot,
                storage,
                FakeScanner(),
                admin_user_ids=frozenset({999}),
                now=200 + 86399,
                registry_lookup=registry_lookup,
            )
        )
        == ()
    )
    assert bot.messages == []


def test_warning_home_pressure_notifies_linked_owner_but_not_admin(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    gib = 1024**3
    storage.save_application(
        Application(
            101, "alice_tg", "Alice Example", "alice", "бессрочно",
            "Andrey Example", "Research", ("brain_lab",),
        )
    )
    storage.save_access(
        101,
        ProvisionedAccess(
            "brain_lab", "alice", "secret", "ssh alice@example", "", True
        ),
    )
    storage.record_disk_sample(
        DiskUsageSample(
            "brain_lab", "dev:42", ("/home",), 1000 * gib, 900 * gib,
            100 * gib, 1000, 900, 100,
        )
    )

    class FakeScanner:
        hosts = ("brain_lab",)

        def top_home(self, _host):
            return (
                DiskDirectoryUsage("/home/alice", 700 * gib),
                DiskDirectoryUsage("/home/bob", 600 * gib),
            )

    class FakeBot:
        messages = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    delivered = asyncio.run(
        deliver_disk_cleanup_requests(
            bot,
            storage,
            FakeScanner(),
            admin_user_ids=frozenset({999}),
            now=200,
            registry_lookup=lambda _username: RegistryMatch(
                "bob_tg", "Bob Example", "bob", "бессрочно",
                "Andrey Example", "Research", ("brain_lab",), 1,
            ),
        )
    )

    assert delivered == (("brain_lab", "alice"),)
    assert [message[0] for message in bot.messages] == [101]
    assert "осталось 10.0%" in bot.messages[0][1]


def test_disk_cleanup_acknowledgement_is_owned_by_telegram_user(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    request = storage.claim_disk_cleanup_request(
        token="token",
        host="brain_lab",
        server_username="alice",
        telegram_user_id=101,
        size_bytes=700 * 1024**3,
        rank=1,
        now=100,
        cooldown_seconds=86400,
    )

    assert request is not None
    assert storage.acknowledge_disk_cleanup_request("token", 999, now=110) is None
    result = storage.acknowledge_disk_cleanup_request("token", 101, now=110)
    assert result is not None
    acknowledged, changed = result
    assert acknowledged.status == "acknowledged"
    assert changed is True

    repeated = storage.acknowledge_disk_cleanup_request("token", 101, now=120)

    assert repeated is not None
    repeated_request, repeated_changed = repeated
    assert repeated_request.acknowledged_at == 110
    assert repeated_changed is False


def test_completed_disk_cleanup_acknowledgement_remains_available_to_owner(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    request = storage.claim_disk_cleanup_request(
        token="token",
        host="brain_lab",
        server_username="alice",
        telegram_user_id=101,
        size_bytes=700 * 1024**3,
        rank=1,
        now=100,
        cooldown_seconds=86400,
    )
    assert request is not None
    storage.complete_disk_cleanup_request(request.token, now=120)

    assert storage.acknowledge_disk_cleanup_request("token", 999, now=130) is None
    result = storage.acknowledge_disk_cleanup_request("token", 101, now=130)

    assert result is not None
    completed, changed = result
    assert completed.status == "completed"
    assert changed is False


def test_reopened_disk_cleanup_request_rotates_callback_token(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    first = storage.claim_disk_cleanup_request(
        token="old-token",
        host="brain_lab",
        server_username="alice",
        telegram_user_id=101,
        size_bytes=700 * 1024**3,
        rank=1,
        now=100,
        cooldown_seconds=86400,
    )
    assert first is not None
    storage.mark_disk_cleanup_notified(first.token, now=100)
    storage.complete_disk_cleanup_request(first.token, now=200)

    reopened = storage.claim_disk_cleanup_request(
        token="new-token",
        host="brain_lab",
        server_username="alice",
        telegram_user_id=101,
        size_bytes=650 * 1024**3,
        rank=2,
        now=200 + 86400,
        cooldown_seconds=86400,
    )

    assert reopened is not None
    assert reopened.token == "new-token"
    assert storage.acknowledge_disk_cleanup_request("old-token", 101, now=200) is None
    assert storage.acknowledge_disk_cleanup_request("new-token", 101, now=200)


def test_failed_disk_cleanup_delivery_is_retried_without_daily_wait(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    gib = 1024**3
    storage.save_application(
        Application(
            telegram_user_id=101,
            telegram_username="alice_tg",
            full_name="Alice Example",
            server_username="alice",
            access_term="бессрочно",
            recommender="Andrey Example",
            activity="Research",
            servers=("brain_lab",),
        )
    )
    storage.save_access(
        101,
        ProvisionedAccess(
            host="brain_lab",
            username="alice",
            password="secret",
            connection="ssh alice@example",
            group_url="",
            created=True,
        ),
    )
    storage.record_disk_sample(
        DiskUsageSample(
            "brain_lab", "dev:42", ("/home",), 1000 * gib, 960 * gib,
            40 * gib, 1000, 900, 100,
        )
    )

    class FakeScanner:
        hosts = ("brain_lab",)

        def top_home(self, _host):
            return (DiskDirectoryUsage("/home/alice", 700 * gib),)

    class FailingBot:
        attempts = 0

        async def send_message(self, *_args, **_kwargs):
            self.attempts += 1
            raise RuntimeError("Telegram unavailable")

    bot = FailingBot()
    for now in (200, 500):
        assert asyncio.run(
            deliver_disk_cleanup_requests(
                bot,
                storage,
                FakeScanner(),
                admin_user_ids=frozenset(),
                now=now,
            )
        ) == ()

    assert bot.attempts == 2


def test_moving_to_sixth_place_without_freeing_space_is_not_success(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    gib = 1024**3
    storage.record_disk_sample(
        DiskUsageSample(
            "brain_lab", "dev:42", ("/home",), 10_000 * gib, 9_600 * gib,
            400 * gib, 1000, 900, 100,
        )
    )
    request = storage.claim_disk_cleanup_request(
        token="token",
        host="brain_lab",
        server_username="alice",
        telegram_user_id=101,
        size_bytes=700 * gib,
        rank=5,
        now=100,
        cooldown_seconds=86400,
    )
    assert request is not None
    storage.mark_disk_cleanup_notified(request.token, now=100)
    assert storage.acknowledge_disk_cleanup_request("token", 101, now=110)

    class FakeScanner:
        hosts = ("brain_lab",)

        def top_home(self, _host):
            return tuple(
                DiskDirectoryUsage(f"/home/user{index}", (800 - index) * gib)
                for index in range(5)
            ) + (DiskDirectoryUsage("/home/alice", 700 * gib),)

    class FakeBot:
        messages = []

        async def send_message(self, *args, **kwargs):
            self.messages.append((args, kwargs))

    bot = FakeBot()
    asyncio.run(
        deliver_disk_cleanup_requests(
            bot,
            storage,
            FakeScanner(),
            admin_user_ids=frozenset(),
            now=200,
        )
    )

    active = storage.disk_cleanup_requests_for_host("brain_lab")
    alice = next(item for item in active if item.server_username == "alice")
    assert alice.status == "acknowledged"
    assert bot.messages == []


def test_disk_owner_lookup_falls_back_to_global_registry_identity(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    match = RegistryMatch(
        telegram_username="alice_tg",
        full_name="Alice Example",
        server_username="alice",
        access_term="бессрочно",
        recommender="Andrey Example",
        activity="Research",
        servers=("brain_lab",),
        source_row_count=1,
        telegram_user_id=101,
    )
    storage.save_registry_match(101, "alice_tg", match)

    assert (
        storage.telegram_user_id_for_host_server_username("brain_lab", "alice")
        == 101
    )
    assert (
        storage.telegram_user_id_for_host_server_username("vv_h200", "alice")
        == 101
    )


def test_disk_owner_lookup_uses_per_host_access_username(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_profile(
        Profile(101, "shkodnik1917", "Andrey Example", "Self")
    )
    storage.save_access(
        101,
        ProvisionedAccess(
            "shkodnik_opt",
            "shkodnik",
            "secret",
            "ssh shkodnik@example.org",
            "",
            False,
        ),
    )

    assert (
        storage.telegram_user_id_for_host_server_username(
            "shkodnik_opt", "shkodnik"
        )
        == 101
    )
    assert storage.telegram_user_id_for_server_username("shkodnik") == 101


def test_disk_status_lists_free_space_and_unreachable_servers() -> None:
    gib = 1024**3
    text = format_disk_status(
        (
            DiskHostStatus(
                host="brain_lab",
                reachable=True,
                filesystems=(
                    DiskFilesystemStatus(
                        key="dev:42",
                        paths=("/", "/home"),
                        total_bytes=1000 * gib,
                        used_bytes=750 * gib,
                        available_bytes=250 * gib,
                        total_inodes=1000,
                        free_inodes=800,
                    ),
                ),
            ),
            DiskHostStatus(host="vv_h200", reachable=False, filesystems=()),
        )
    )

    assert "🟢 <b>Новый сервер (BrainLab)</b>" in text
    assert "/ и /home · занято 75.0% · свободно 250.0 GB" in text
    assert "🔴 <b>H200</b>" in text
    assert "Сервер недоступен" in text
