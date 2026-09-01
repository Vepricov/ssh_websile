import asyncio
import html
import json
import logging
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .domain import DiskUsageSample
from .monitoring import PUBLIC_SERVER_NAMES
from .storage import Storage

Runner = Callable[[str, tuple[str, ...]], tuple[int, str]]
HELPER = "/usr/local/sbin/hermes-disk-report"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiskFilesystemStatus:
    key: str
    paths: tuple[str, ...]
    total_bytes: int
    used_bytes: int
    available_bytes: int
    total_inodes: int
    free_inodes: int


@dataclass(frozen=True)
class DiskHostStatus:
    host: str
    reachable: bool
    filesystems: tuple[DiskFilesystemStatus, ...]


@dataclass(frozen=True)
class DiskDirectoryUsage:
    path: str
    size_bytes: int


@dataclass(frozen=True)
class DiskCleanupAction:
    name: str
    freed_bytes: int
    status: str = "ok"


@dataclass(frozen=True)
class DiskCleanupResult:
    before_available_bytes: int
    after_available_bytes: int
    actions: tuple[DiskCleanupAction, ...]


@dataclass(frozen=True)
class DiskAlert:
    level: str
    sample: DiskUsageSample
    growth_since_previous_bytes: int | None
    growth_since_day_bytes: int | None
    day_baseline_at: int | None


class DiskScanner:
    SUMMARY_QUERY = ("sudo", "-n", HELPER, "summary")
    TOP_HOME_QUERY = ("sudo", "-n", HELPER, "top-home")
    CLEANUP_ROOT_QUERY = ("sudo", "-n", HELPER, "cleanup-root")

    def __init__(
        self,
        hosts: Sequence[str],
        runner: Runner | None = None,
    ) -> None:
        self.hosts = tuple(hosts)
        self.runner = runner or self._run_ssh

    @staticmethod
    def _run_ssh(host: str, command: tuple[str, ...]) -> tuple[int, str]:
        timeout_seconds = 150 if command == DiskScanner.CLEANUP_ROOT_QUERY else 30
        completed = subprocess.run(
            [
                "/usr/bin/ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "--",
                host,
                shlex.join(command),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": "/root",
                "LANG": "C.UTF-8",
            },
        )
        return completed.returncode, completed.stdout

    def scan(self) -> tuple[DiskHostStatus, ...]:
        def safe_run(host: str) -> tuple[int, str]:
            try:
                return self.runner(host, self.SUMMARY_QUERY)
            except (OSError, subprocess.SubprocessError, TimeoutError):
                return 255, ""

        with ThreadPoolExecutor(max_workers=max(1, len(self.hosts))) as executor:
            futures = {host: executor.submit(safe_run, host) for host in self.hosts}

        statuses = []
        for host in self.hosts:
            return_code, output = futures[host].result()
            filesystems = self._parse_summary(output) if return_code == 0 else ()
            statuses.append(
                DiskHostStatus(
                    host=host,
                    reachable=return_code == 0 and filesystems is not None,
                    filesystems=filesystems or (),
                )
            )
        return tuple(statuses)

    def top_home(self, host: str) -> tuple[DiskDirectoryUsage, ...]:
        if host not in self.hosts:
            return ()
        try:
            return_code, output = self.runner(host, self.TOP_HOME_QUERY)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            return ()
        if return_code != 0:
            return ()
        try:
            payload = json.loads(output)
            rows = payload["directories"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return ()
        if not isinstance(rows, list):
            return ()
        directories = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                return ()
            path = row.get("path")
            size = row.get("size_bytes")
            if (
                not isinstance(path, str)
                or not path.startswith("/home/")
                or type(size) is not int
                or size < 0
            ):
                return ()
            directories.append(DiskDirectoryUsage(path, size))
        return tuple(directories)

    def cleanup_root(self, host: str) -> DiskCleanupResult | None:
        if host not in self.hosts:
            return None
        try:
            return_code, output = self.runner(host, self.CLEANUP_ROOT_QUERY)
        except (OSError, subprocess.SubprocessError, TimeoutError):
            return None
        if return_code != 0:
            return None
        try:
            payload = json.loads(output)
            before = payload["before_available_bytes"]
            after = payload["after_available_bytes"]
            rows = payload["actions"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if (
            type(before) is not int
            or before < 0
            or type(after) is not int
            or after < 0
            or not isinstance(rows, list)
        ):
            return None
        actions = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            name = row.get("name")
            freed = row.get("freed_bytes")
            status = row.get("status")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 80
                or type(freed) is not int
                or freed < 0
                or status not in {"ok", "error", "timeout", "busy"}
            ):
                return None
            actions.append(DiskCleanupAction(name, freed, status))
        return DiskCleanupResult(before, after, tuple(actions))

    @staticmethod
    def _parse_summary(text: str) -> tuple[DiskFilesystemStatus, ...] | None:
        try:
            payload = json.loads(text)
            rows = payload["filesystems"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        if not isinstance(rows, list):
            return None
        filesystems = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            paths = row.get("paths")
            if (
                not isinstance(paths, list)
                or not paths
                or any(path not in {"/", "/home"} for path in paths)
            ):
                return None
            values = [
                row.get("total_bytes"),
                row.get("used_bytes"),
                row.get("available_bytes"),
                row.get("total_inodes"),
                row.get("free_inodes"),
            ]
            if any(type(value) is not int or value < 0 for value in values):
                return None
            key = row.get("key")
            if not isinstance(key, str) or not key.startswith("dev:"):
                return None
            filesystems.append(
                DiskFilesystemStatus(
                    key=key,
                    paths=tuple(paths),
                    total_bytes=values[0],
                    used_bytes=values[1],
                    available_bytes=values[2],
                    total_inodes=values[3],
                    free_inodes=values[4],
                )
            )
        return tuple(filesystems)


class DiskMonitor:
    def __init__(
        self,
        storage: Storage,
        scanner: DiskScanner,
        *,
        warning_free_percent: int = 15,
        critical_free_percent: int = 5,
        notification_cooldown_seconds: int = 21600,
    ) -> None:
        self.storage = storage
        self.scanner = scanner
        self.warning_free_percent = warning_free_percent
        self.critical_free_percent = critical_free_percent
        self.notification_cooldown_seconds = notification_cooldown_seconds

    def check(self, *, now: int) -> tuple[DiskAlert, ...]:
        alerts = []
        for host_status in self.scanner.scan():
            if not host_status.reachable:
                continue
            for filesystem in host_status.filesystems:
                sample = DiskUsageSample(
                    host=host_status.host,
                    filesystem_key=filesystem.key,
                    paths=filesystem.paths,
                    total_bytes=filesystem.total_bytes,
                    used_bytes=filesystem.used_bytes,
                    available_bytes=filesystem.available_bytes,
                    total_inodes=filesystem.total_inodes,
                    free_inodes=filesystem.free_inodes,
                    sampled_at=now,
                )
                previous, day = self.storage.record_disk_sample(sample)
                free_percent = (
                    100 * sample.available_bytes / sample.total_bytes
                    if sample.total_bytes
                    else 0
                )
                inode_free_percent = (
                    100 * sample.free_inodes / sample.total_inodes
                    if sample.total_inodes
                    else 100
                )
                minimum_free_percent = min(free_percent, inode_free_percent)
                if minimum_free_percent <= self.critical_free_percent:
                    level = "critical"
                elif minimum_free_percent <= self.warning_free_percent:
                    level = "warning"
                else:
                    level = "normal"
                if not self.storage.claim_disk_alert(
                    sample.host,
                    sample.filesystem_key,
                    level,
                    now=now,
                    cooldown_seconds=self.notification_cooldown_seconds,
                ):
                    continue
                alerts.append(
                    DiskAlert(
                        level=level,
                        sample=sample,
                        growth_since_previous_bytes=(
                            None
                            if previous is None
                            else sample.used_bytes - previous.used_bytes
                        ),
                        growth_since_day_bytes=(
                            None if day is None else sample.used_bytes - day.used_bytes
                        ),
                        day_baseline_at=None if day is None else day.sampled_at,
                    )
                )
        return tuple(alerts)


def _format_bytes(value: int, *, signed: bool = False) -> str:
    sign = "+" if signed and value >= 0 else ""
    absolute = abs(value)
    if absolute >= 1024**4:
        return f"{sign}{value / 1024**4:.2f} TB"
    return f"{sign}{value / 1024**3:.1f} GB"


def format_disk_alert(
    alerts: Sequence[DiskAlert],
    top_home: Sequence[DiskDirectoryUsage] = (),
    cleanup: DiskCleanupResult | None = None,
) -> str:
    host = alerts[0].sample.host
    server_name = html.escape(PUBLIC_SERVER_NAMES.get(host, host))
    critical = any(alert.level == "critical" for alert in alerts)
    sections = [
        ("🆘" if critical else "⚠️")
        + f" <b>{'Критически мало' if critical else 'Мало'} места</b>",
        f"<b>{server_name}</b>",
    ]
    for alert in alerts:
        sample = alert.sample
        paths = " и ".join(sample.paths)
        used_percent = (
            100 * sample.used_bytes / sample.total_bytes if sample.total_bytes else 100
        )
        inode_free_percent = (
            100 * sample.free_inodes / sample.total_inodes
            if sample.total_inodes
            else 100
        )
        lines = [
            f"<b>{html.escape(paths)}</b>",
            f"Занято: {used_percent:.1f}% из {_format_bytes(sample.total_bytes)}",
            f"Свободно: <b>{_format_bytes(sample.available_bytes)}</b>",
            f"Свободные inode: {inode_free_percent:.1f}%",
        ]
        if alert.growth_since_previous_bytes is not None:
            lines.append(
                "С прошлого замера: "
                + _format_bytes(alert.growth_since_previous_bytes, signed=True)
            )
        if alert.growth_since_day_bytes is not None:
            label = (
                "За 24 часа"
                if alert.day_baseline_at is not None
                and sample.sampled_at - alert.day_baseline_at >= 20 * 3600
                else "С начала наблюдения"
            )
            lines.append(
                f"{label}: {_format_bytes(alert.growth_since_day_bytes, signed=True)}"
            )
        sections.append("\n".join(lines))
    if top_home:
        top_lines = ["<b>Самые большие каталоги в /home</b>"]
        top_lines.extend(
            f"<code>{html.escape(item.path)}</code> · {_format_bytes(item.size_bytes)}"
            for item in top_home
        )
        sections.append("\n".join(top_lines))
    elif any("/home" in alert.sample.paths for alert in alerts):
        sections.append(
            "<b>Top /home</b>\n"
            "Быстрая quota-статистика пока недоступна или домашние каталоги пусты."
        )
    if cleanup is not None:
        cleanup_lines = [
            "<b>Безопасная очистка системного раздела</b>",
            f"До очистки: {_format_bytes(cleanup.before_available_bytes)} свободно",
        ]
        cleanup_lines.extend(
            f"{html.escape(action.name)}: −{_format_bytes(action.freed_bytes)}"
            for action in cleanup.actions
            if action.freed_bytes > 0
        )
        status_names = {
            "error": "ошибка",
            "timeout": "превышено время ожидания",
            "busy": "другая очистка уже выполняется",
        }
        cleanup_lines.extend(
            f"{html.escape(action.name)}: {status_names[action.status]}"
            for action in cleanup.actions
            if action.status != "ok"
        )
        cleanup_lines.append(
            f"После очистки: <b>{_format_bytes(cleanup.after_available_bytes)}</b> "
            "свободно"
        )
        cleanup_lines.append(
            "Автоматически больше ничего не удаляю. Если места всё ещё мало, "
            "нужна ручная проверка."
        )
        sections.append("\n".join(cleanup_lines))
    return "\n\n".join(sections)


def format_disk_status(statuses: Sequence[DiskHostStatus]) -> str:
    sections = ["💾 <b>Свободное место на серверах</b>"]
    for status in statuses:
        server_name = html.escape(PUBLIC_SERVER_NAMES.get(status.host, status.host))
        if not status.reachable:
            sections.append(f"🔴 <b>{server_name}</b>\nСервер недоступен.")
            continue
        lines = [f"🟢 <b>{server_name}</b>"]
        for filesystem in status.filesystems:
            paths = html.escape(" и ".join(filesystem.paths))
            used_percent = (
                100 * filesystem.used_bytes / filesystem.total_bytes
                if filesystem.total_bytes
                else 100
            )
            lines.append(
                f"{paths} · занято {used_percent:.1f}% · свободно "
                f"{_format_bytes(filesystem.available_bytes)}"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


async def deliver_disk_alerts(
    bot: object,
    storage: Storage,
    scanner: DiskScanner,
    alerts: Sequence[DiskAlert],
    *,
    admin_user_ids: frozenset[int],
) -> tuple[str, ...]:
    grouped: dict[str, list[DiskAlert]] = {}
    for alert in alerts:
        grouped.setdefault(alert.sample.host, []).append(alert)
    delivered = []
    for host, host_alerts in grouped.items():
        top_home = await asyncio.to_thread(scanner.top_home, host)
        cleanup = None
        if any("/" in alert.sample.paths for alert in host_alerts):
            cleanup = await asyncio.to_thread(scanner.cleanup_root, host)
        text = format_disk_alert(host_alerts, top_home, cleanup)
        host_delivered = False
        for admin_user_id in admin_user_ids:
            try:
                await bot.send_message(admin_user_id, text)
                host_delivered = True
            except Exception:
                LOGGER.exception(
                    "Could not send disk alert for %s to admin %s",
                    host,
                    admin_user_id,
                )
        if host_delivered:
            delivered.append(host)
    return tuple(delivered)


async def run_disk_monitor(
    bot: object,
    monitor: DiskMonitor,
    scanner: DiskScanner,
    *,
    interval_seconds: int,
    admin_user_ids: frozenset[int],
) -> None:
    while True:
        started = time.monotonic()
        try:
            alerts = await asyncio.to_thread(monitor.check, now=int(time.time()))
            if alerts:
                await deliver_disk_alerts(
                    bot,
                    monitor.storage,
                    scanner,
                    alerts,
                    admin_user_ids=admin_user_ids,
                )
        except Exception:
            LOGGER.exception("Disk monitor iteration failed")
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1, interval_seconds - elapsed))
