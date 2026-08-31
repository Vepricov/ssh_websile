import asyncio
import json

from server_access_bot.disk_monitoring import (
    DiskAlert,
    DiskDirectoryUsage,
    DiskFilesystemStatus,
    DiskHostStatus,
    DiskMonitor,
    DiskScanner,
    deliver_disk_alerts,
    format_disk_status,
)
from server_access_bot.domain import DiskUsageSample, ServerChatBinding
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


def test_disk_monitor_alerts_on_warning_and_escalates_to_critical(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.bind_server_chat("brain_lab", -100123, None, 101)

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


def test_disk_alert_is_sent_to_bound_topic_with_growth_and_top_home(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.bind_server_chat("brain_lab", -100123, 17, 101)
    gib = 1024**3
    alert = DiskAlert(
        level="warning",
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

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, **kwargs):
            self.messages.append((chat_id, text, kwargs))

    bot = FakeBot()
    delivered = asyncio.run(
        deliver_disk_alerts(bot, storage, FakeScanner(), (alert,))
    )

    assert delivered == ("brain_lab",)
    assert bot.messages[0][0] == -100123
    assert bot.messages[0][2]["message_thread_id"] == 17
    text = bot.messages[0][1]
    assert "Новый сервер (BrainLab)" in text
    assert "/ и /home" in text
    assert "Свободно: <b>100.0 GB</b>" in text
    assert "С прошлого замера: +12.0 GB" in text
    assert "За 24 часа: +85.0 GB" in text
    assert "<code>/home/alice</code> · 400.0 GB" in text


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
