import asyncio
import base64

from server_access_bot.config import Settings
from server_access_bot.domain import Application, GpuIdleCandidate, GpuIdleIncident
from server_access_bot.monitoring import (
    GpuFleetScanner,
    deliver_gpu_idle_incidents,
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
    assert bot.messages[0][0] == 101
    assert "Новый сервер" in bot.messages[0][1]
    assert "brain_lab" not in bot.messages[0][1]
    assert "9.0 GB" in bot.messages[0][1]
    assert "PID: <code>101, 102</code>" in bot.messages[0][1]
    assert format_user_idle_notification((incident,)) == bot.messages[0][1]


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
