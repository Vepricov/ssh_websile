import asyncio
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

from server_access_bot.config import Settings
from server_access_bot.domain import (
    Application,
    GpuIdleCandidate,
    GpuIdleIncident,
    ProcessIdentity,
    RegistryMatch,
)
from server_access_bot.monitoring import (
    DRIVER_HUNG_EXIT_CODE,
    PUBLIC_SERVER_NAMES,
    GpuFleetScanner,
    GpuIdleMonitor,
    GpuIdlePolicy,
    GpuScan,
    GpuStatusScanner,
    confirmed_driver_hangs,
    deliver_driver_hang_alerts,
    deliver_gpu_idle_incidents,
    format_gpu_status,
    format_user_idle_notification,
)
from server_access_bot.storage import Storage


def test_scanner_finds_user_holding_vram_on_an_idle_gpu() -> None:
    gpu_output = (
        "0, GPU-aaaa, NVIDIA A100, 81920, 12000, 69920, 1\n"
        "1, GPU-bbbb, NVIDIA A100, 81920, 20000, 61920, 80\n"
    )
    process_output = (
        "GPU-aaaa\t101\t5000\talice\t7200\n"
        "GPU-aaaa\t102\t4000\talice\t7100\n"
        "GPU-bbbb\t201\t20000\tbob\t7200\n"
    )

    def runner(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        assert host == "brain_lab"
        if command == GpuFleetScanner.GPU_QUERY:
            return 0, gpu_output
        return 0, process_output

    scan = GpuFleetScanner(
        hosts=("brain_lab",),
        memory_threshold_mb=4096,
        utilization_threshold_percent=2,
        runner=runner,
    ).scan()

    assert scan.reachable_hosts == ("brain_lab",)
    assert len(scan.candidates) == 1
    candidate = scan.candidates[0]
    assert candidate.host == "brain_lab"
    assert candidate.gpu_index == 0
    assert candidate.server_username == "alice"
    assert candidate.used_memory_mb == 9000
    assert candidate.pids == (101, 102)


def test_gpu_status_shows_each_card_and_unreachable_server() -> None:
    gpu_output = (
        "0, GPU-aaaa, NVIDIA A100 80GB PCIe, 81920, 24576, 57344, 31\n"
        "1, GPU-bbbb, NVIDIA A100 80GB PCIe, 81920, 1024, 80896, 0\n"
    )

    def runner(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        assert command == GpuStatusScanner.GPU_QUERY
        return (0, gpu_output) if host == "brain_lab" else (255, "")

    statuses = GpuStatusScanner(
        hosts=("brain_lab", "vv_h200"), runner=runner
    ).scan()
    text = format_gpu_status(
        statuses,
        updated_at=datetime(2026, 8, 31, 9, 12, tzinfo=ZoneInfo("Europe/Moscow")),
    )

    assert "🟢 <b>Новый сервер (BrainLab)</b>" in text
    assert "Обновлено: 31.08.2026, 09:12 МСК" in text
    assert "GPU 0 · NVIDIA A100 80GB PCIe" in text
    assert (
        "<code>UTIL ██░░░ 31% · VRAM ██░░░ 30% · 24.0/80.0 GB</code>"
        in text
    )
    assert "GPU 1" in text
    assert "🔴 <b>H200</b>" in text
    assert "Сервер недоступен по SSH" in text


def test_yandex_is_not_a_monitored_server() -> None:
    assert "yandex" not in PUBLIC_SERVER_NAMES


def test_server_status_shows_alibaba_ppu_allocation() -> None:
    def runner(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        assert host == "alibaba_ascend"
        assert command == GpuStatusScanner.ALIBABA_QUERY
        return 0, "10|9|up|mix\n"

    statuses = GpuStatusScanner(
        hosts=("alibaba_ascend",), runner=runner
    ).scan()
    text = format_gpu_status(statuses)

    assert "🟢 <b>Alibaba</b>" in text
    assert "PPU █████ 9/10 занято (90%)" in text
    assert "Slurm partition: brain · состояние: mix" in text


def test_idle_incident_requires_full_duration_and_has_durable_cooldown(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    candidate = GpuIdleCandidate(
        host="brain_lab",
        gpu_uuid="GPU-aaaa",
        gpu_index=0,
        server_username="alice",
        used_memory_mb=9000,
        pids=(101, 102),
    )

    for now in range(0, 1800, 300):
        incidents = storage.update_gpu_idle_observations(
            (candidate,),
            reachable_hosts=("brain_lab",),
            now=now,
            minimum_duration_seconds=1800,
            minimum_samples=6,
            maximum_sample_gap_seconds=600,
            notification_cooldown_seconds=43200,
        )
        assert incidents == []

    incidents = storage.update_gpu_idle_observations(
        (candidate,),
        reachable_hosts=("brain_lab",),
        now=1800,
        minimum_duration_seconds=1800,
        minimum_samples=6,
        maximum_sample_gap_seconds=600,
        notification_cooldown_seconds=43200,
    )

    assert len(incidents) == 1
    storage.mark_gpu_idle_notified((incidents[0].key,), now=1800)
    assert storage.update_gpu_idle_observations(
        (candidate,),
        reachable_hosts=("brain_lab",),
        now=2100,
        minimum_duration_seconds=1800,
        minimum_samples=6,
        maximum_sample_gap_seconds=600,
        notification_cooldown_seconds=43200,
    ) == []


def test_server_username_mapping_fails_closed_when_ambiguous(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)

    def application(telegram_user_id: int) -> Application:
        return Application(
            telegram_user_id=telegram_user_id,
            telegram_username=f"alice{telegram_user_id}",
            full_name="Alice Smith",
            server_username="alice",
            access_term="бессрочно",
            recommender="Bob Jones",
            activity="research",
            servers=("brain_lab",),
        )

    storage.save_application(application(101))
    assert storage.telegram_user_id_for_server_username("alice") == 101

    storage.save_application(application(202))
    assert storage.telegram_user_id_for_server_username("alice") is None
    assert storage.telegram_user_id_for_server_username("unknown") is None


def test_notification_uses_public_server_name_and_marks_delivery(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_application(
        Application(
            telegram_user_id=101,
            telegram_username="alice_tg",
            full_name="Alice Smith",
            server_username="alice",
            access_term="бессрочно",
            recommender="Bob Jones",
            activity="research",
            servers=("brain_lab",),
        )
    )
    incident = GpuIdleIncident(
        key="brain_lab:GPU-aaaa:alice",
        host="brain_lab",
        gpu_index=0,
        server_username="alice",
        used_memory_mb=9216,
        pids=(101, 102),
        first_seen_at=0,
        last_seen_at=1800,
    )

    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    class FakeController:
        def snapshot(self, host, username, pids):
            assert (host, username, pids) == ("brain_lab", "alice", (101, 102))
            return (ProcessIdentity(101, 1001), ProcessIdentity(102, 1002))

    delivered = asyncio.run(
        deliver_gpu_idle_incidents(
            bot,
            storage,
            (incident,),
            admin_user_ids=frozenset({999}),
            now=1800,
            process_controller=FakeController(),
        )
    )

    assert delivered == (incident.key,)
    assert bot.messages[0][0] == 101
    assert "Новый сервер" in bot.messages[0][1]
    assert "brain_lab" not in bot.messages[0][1]
    assert "9.0 GB" in bot.messages[0][1]
    assert "PID: <code>101, 102</code>" in bot.messages[0][1]
    assert format_user_idle_notification((incident,)) == bot.messages[0][1]
    assert bot.messages[0][2]["protect_content"] is False
    keyboard = bot.messages[0][2]["reply_markup"]
    callback_data = keyboard.inline_keyboard[0][0].callback_data
    assert callback_data.startswith("gpu:stop:")
    token = callback_data.rsplit(":", 1)[-1]
    action = storage.gpu_process_action_for(token, 101, now=10**12)
    assert action is not None
    assert action.identities == (
        ProcessIdentity(101, 1001),
        ProcessIdentity(102, 1002),
    )


def test_monitor_policy_defaults_are_loaded_from_environment(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ACCESS_BOT_TOKEN", "token")
    monkeypatch.setenv(
        "ACCESS_BOT_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"x" * 32).decode(),
    )
    monkeypatch.setenv("ACCESS_BOT_DATABASE", str(tmp_path / "bot.sqlite3"))

    settings = Settings.from_env()

    assert settings.gpu_monitor_enabled is True
    assert settings.gpu_monitor_interval_seconds == 300
    assert settings.gpu_idle_memory_threshold_mb == 4096
    assert settings.gpu_idle_utilization_threshold_percent == 2
    assert settings.gpu_idle_minimum_duration_seconds == 1800
    assert settings.gpu_idle_notification_cooldown_seconds == 43200
    assert settings.disk_monitor_enabled is True
    assert settings.disk_monitor_interval_seconds == 300
    assert settings.disk_warning_free_percent == 15
    assert settings.disk_critical_free_percent == 5
    assert settings.disk_notification_cooldown_seconds == 21600


def test_unmapped_server_username_is_reported_only_to_admin(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    incident = GpuIdleIncident(
        key="vv_h200:GPU-aaaa:unknown",
        host="vv_h200",
        gpu_index=7,
        server_username="unknown",
        used_memory_mb=20480,
        pids=(303,),
        first_seen_at=0,
        last_seen_at=1800,
    )

    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    delivered = asyncio.run(
        deliver_gpu_idle_incidents(
            bot,
            storage,
            (incident,),
            admin_user_ids=frozenset({999}),
            now=1800,
        )
    )

    assert delivered == (incident.key,)
    assert [message[0] for message in bot.messages] == [999]
    assert "не связан с Telegram ID" in bot.messages[0][1]
    assert "unknown" in bot.messages[0][1]
    assert "Кому написать" in bot.messages[0][1]
    assert "в общей беседе или таблице" in bot.messages[0][1]
    assert "Привет! На H200" in bot.messages[0][1]
    assert "GPU 7" in bot.messages[0][1]
    assert "20.0 GB VRAM" in bot.messages[0][1]
    assert "PID: 303" in bot.messages[0][1]
    assert "не менее 30 минут" in bot.messages[0][1]
    assert bot.messages[0][2]["protect_content"] is False
    assert "reply_markup" not in bot.messages[0][2]


def test_unmapped_owner_contact_is_loaded_from_sheet_for_admin(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    incident = GpuIdleIncident(
        key="yandex:GPU-aaaa:akzium",
        host="yandex",
        gpu_index=1,
        server_username="akzium",
        used_memory_mb=8192,
        pids=(303,),
        first_seen_at=0,
        last_seen_at=1800,
    )

    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            self.messages.append((chat_id, text, kwargs))

    def registry_lookup(server_username: str) -> RegistryMatch:
        assert server_username == "akzium"
        return RegistryMatch(
            telegram_username="postscripter",
            full_name="Peter Badolya",
            server_username="akzium",
            access_term="бессрочно",
            recommender="Bob Jones",
            activity="experiments",
            servers=("shkodnik_opt",),
            source_row_count=1,
            telegram_user_id=None,
        )

    bot = FakeBot()
    asyncio.run(
        deliver_gpu_idle_incidents(
            bot,
            storage,
            (incident,),
            admin_user_ids=frozenset({999}),
            now=1800,
            registry_lookup=registry_lookup,
        )
    )

    assert [message[0] for message in bot.messages] == [999]
    assert "Telegram: @postscripter" in bot.messages[0][1]
    assert "Unix username: <code>akzium</code>" in bot.messages[0][1]


def test_trusted_sheet_owner_is_linked_and_notified_directly(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    incident = GpuIdleIncident(
        key="yandex:GPU-aaaa:akzium",
        host="yandex",
        gpu_index=1,
        server_username="akzium",
        used_memory_mb=8192,
        pids=(303,),
        first_seen_at=0,
        last_seen_at=1800,
    )
    match = RegistryMatch(
        telegram_username="postscripter",
        full_name="Peter Badolya",
        server_username="akzium",
        access_term="бессрочно",
        recommender="Bob Jones",
        activity="experiments",
        servers=("shkodnik_opt",),
        source_row_count=1,
        telegram_user_id=4242,
    )

    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    asyncio.run(
        deliver_gpu_idle_incidents(
            bot,
            storage,
            (incident,),
            admin_user_ids=frozenset({999}),
            now=1800,
            registry_lookup=lambda username: match,
        )
    )

    assert [message[0] for message in bot.messages] == [4242]
    assert storage.telegram_user_id_for_server_username("akzium") == 4242


def test_admin_notification_uses_natural_server_phrase(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    incident = GpuIdleIncident(
        key="brain_lab:GPU-aaaa:unknown",
        host="brain_lab",
        gpu_index=3,
        server_username="unknown",
        used_memory_mb=44237,
        pids=(2805527, 3515658),
        first_seen_at=0,
        last_seen_at=1800,
    )

    class FakeBot:
        def __init__(self) -> None:
            self.messages = []

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    asyncio.run(
        deliver_gpu_idle_incidents(
            bot,
            storage,
            (incident,),
            admin_user_ids=frozenset({999}),
            now=1800,
        )
    )

    text = bot.messages[0][1]
    assert "Привет! На новом сервере (BrainLab)" in text
    assert "На сервере Новый сервер" not in text


HUNG_OUTPUT = (
    "DRIVER_HUNG stuck=191\n"
    "[Tue Oct  6 15:35:06 2026] NVRM: nvAssertFailedNoLog: Assertion failed: "
    "rmGpuLocksGetOwnedMask() == 0 @ rmapi.c:566\n"
)


def test_fleet_scanner_reports_hung_driver_instead_of_candidates() -> None:
    def runner(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        return DRIVER_HUNG_EXIT_CODE, HUNG_OUTPUT

    scan = GpuFleetScanner(
        hosts=("shkodnik_opt",),
        memory_threshold_mb=4096,
        utilization_threshold_percent=2,
        runner=runner,
    ).scan()

    assert scan.reachable_hosts == ()
    assert scan.candidates == ()
    assert scan.driver_hung[0][0] == "shkodnik_opt"
    assert "rmGpuLocksGetOwnedMask" in scan.driver_hung[0][1]


def test_nvidia_queries_never_start_while_a_call_is_stuck() -> None:
    for command in (GpuFleetScanner.GPU_QUERY, GpuFleetScanner.PROCESS_QUERY):
        script = command[2]
        assert script.index("stuck=") < script.index("nvidia-smi --query")
        assert "$3 >= 120" in script
        assert "for i in 1 2 3 4 5" in script
        assert f"exit {DRIVER_HUNG_EXIT_CODE}" in script
        assert 'kill -9 "$p"' in script


def test_status_shows_hung_driver_not_ssh_failure() -> None:
    def runner(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        return DRIVER_HUNG_EXIT_CODE, HUNG_OUTPUT

    statuses = GpuStatusScanner(hosts=("shkodnik_opt",), runner=runner).scan()
    text = format_gpu_status(statuses)

    assert statuses[0].driver_hung is True
    assert "🟡 <b>Старый сервер (MIPT)</b>" in text
    assert "не ответил вовремя" in text
    assert "недоступен по SSH" not in text


def test_driver_hang_alert_is_sent_once_then_recovery(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)

    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
            self.sent.append((chat_id, text))

    bot = FakeBot()
    hung = (("shkodnik_opt", HUNG_OUTPUT.strip()),)
    admins = frozenset({1})

    def deliver(hung_hosts, reachable, now):
        asyncio.run(
            deliver_driver_hang_alerts(
                bot, storage, hung_hosts, reachable, admin_user_ids=admins, now=now
            )
        )

    deliver(hung, ("brain_lab",), 1000)
    deliver(hung, ("brain_lab",), 1300)
    deliver((), ("brain_lab", "shkodnik_opt"), 1600)
    deliver((), ("brain_lab", "shkodnik_opt"), 1900)

    assert len(bot.sent) == 2
    assert "драйвер NVIDIA завис" in bot.sent[0][1]
    assert "rmGpuLocksGetOwnedMask" in bot.sent[0][1]
    assert "снова отвечает" in bot.sent[1][1]


def test_one_slow_nvidia_smi_is_not_a_confirmed_hang(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    results = iter(["hung", "ok", "hung", "hung", "hung"])

    class FakeScanner:
        def scan(self):
            if next(results) == "hung":
                return GpuScan((), (), (("vv_h200", "DRIVER_HUNG timeout"),))
            return GpuScan(("vv_h200",), ())

    monitor = GpuIdleMonitor(storage, FakeScanner(), GpuIdlePolicy())
    confirmed = []
    for now in range(1000, 1500, 100):
        monitor.check(now=now)
        confirmed.append(bool(confirmed_driver_hangs(monitor)))

    assert confirmed == [False, False, False, False, True]


def test_guard_waits_out_a_slow_but_alive_nvidia_smi(tmp_path) -> None:
    import os
    import subprocess

    shim = tmp_path / "nvidia-smi"
    shim.write_text("#!/bin/sh\nsleep 3\necho 0, GPU-x, A100, 81920, 1, 81919, 0\n")
    shim.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", "-c", GpuFleetScanner.GPU_QUERY[2]],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )

    assert result.returncode == 0
    assert "GPU-x" in result.stdout
