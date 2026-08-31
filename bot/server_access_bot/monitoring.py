import asyncio
import html
import logging
import re
import secrets
import shlex
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .domain import GpuIdleCandidate, GpuIdleIncident, GpuProcessAction
from .process_control import (
    PROCESS_ACTION_NEVER_EXPIRES_AT,
    GpuProcessController,
    ProcessControlError,
)
from .storage import Storage

Runner = Callable[[str, tuple[str, ...]], tuple[int, str]]
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
LOGGER = logging.getLogger(__name__)
PUBLIC_SERVER_NAMES = {
    "shkodnik_opt": "Старый сервер (MIPT)",
    "brain_lab": "Новый сервер (BrainLab)",
    "vv_h200": "H200",
}
SERVER_LOCATIONS = {
    "shkodnik_opt": "старом сервере (MIPT)",
    "brain_lab": "новом сервере (BrainLab)",
    "vv_h200": "H200",
}


@dataclass(frozen=True)
class GpuScan:
    reachable_hosts: tuple[str, ...]
    candidates: tuple[GpuIdleCandidate, ...]


@dataclass(frozen=True)
class GpuDeviceStatus:
    index: int
    name: str
    total_memory_mb: int
    used_memory_mb: int
    utilization_percent: int


@dataclass(frozen=True)
class ServerGpuStatus:
    host: str
    reachable: bool
    devices: tuple[GpuDeviceStatus, ...]


@dataclass(frozen=True)
class GpuIdlePolicy:
    interval_seconds: int = 300
    memory_threshold_mb: int = 4096
    utilization_threshold_percent: int = 2
    minimum_duration_seconds: int = 1800
    minimum_samples: int = 6
    notification_cooldown_seconds: int = 43200

    def __post_init__(self) -> None:
        values = (
            self.interval_seconds,
            self.memory_threshold_mb,
            self.minimum_duration_seconds,
            self.minimum_samples,
            self.notification_cooldown_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("GPU idle monitor policy values must be positive")
        if not 0 <= self.utilization_threshold_percent <= 100:
            raise ValueError("GPU utilization threshold must be between 0 and 100")


class GpuFleetScanner:
    GPU_QUERY = (
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
    )
    PROCESS_QUERY = (
        "/bin/sh",
        "-lc",
        "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory "
        "--format=csv,noheader,nounits 2>/dev/null | "
        "while IFS=, read -r uuid pid memory; do "
        "uuid=$(printf '%s' \"$uuid\" | tr -d ' '); "
        "pid=$(printf '%s' \"$pid\" | tr -d ' '); "
        "memory=$(printf '%s' \"$memory\" | tr -d ' '); "
        "case $pid in ''|*[!0-9]*) continue;; esac; "
        "meta=$(ps -o user:32=,etimes= -p \"$pid\" 2>/dev/null | head -n 1); "
        "user=$(printf '%s' \"$meta\" | awk '{print $1}'); "
        "elapsed=$(printf '%s' \"$meta\" | awk '{print $2}'); "
        "printf '%s\\t%s\\t%s\\t%s\\t%s\\n' "
        "\"$uuid\" \"$pid\" \"$memory\" \"$user\" \"$elapsed\"; done",
    )

    def __init__(
        self,
        hosts: Sequence[str],
        memory_threshold_mb: int,
        utilization_threshold_percent: int,
        runner: Runner | None = None,
    ) -> None:
        self.hosts = tuple(hosts)
        self.memory_threshold_mb = memory_threshold_mb
        self.utilization_threshold_percent = utilization_threshold_percent
        self.runner = runner or self._run_ssh

    @staticmethod
    def _run_ssh(host: str, command: tuple[str, ...]) -> tuple[int, str]:
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
            timeout=20,
            check=False,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": "/root",
                "LANG": "C.UTF-8",
            },
        )
        return completed.returncode, completed.stdout

    @staticmethod
    def _non_negative_integer(value: str) -> int | None:
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None

    def scan(self) -> GpuScan:
        requests = [
            (host, command)
            for host in self.hosts
            for command in (self.GPU_QUERY, self.PROCESS_QUERY)
        ]

        def safe_run(host: str, command: tuple[str, ...]) -> tuple[int, str]:
            try:
                return self.runner(host, command)
            except (OSError, subprocess.SubprocessError, TimeoutError):
                return 255, ""

        with ThreadPoolExecutor(max_workers=max(1, len(requests))) as executor:
            futures = {
                (host, command): executor.submit(safe_run, host, command)
                for host, command in requests
            }

        reachable = []
        candidates = []
        for host in self.hosts:
            gpu_rc, gpu_text = futures[(host, self.GPU_QUERY)].result()
            process_rc, process_text = futures[(host, self.PROCESS_QUERY)].result()
            if gpu_rc != 0 or process_rc != 0:
                continue
            reachable.append(host)
            gpus = self._parse_gpus(gpu_text)
            processes = self._parse_processes(process_text)
            grouped: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
            for gpu_uuid, pid, memory, username in processes:
                grouped[(gpu_uuid, username)].append((pid, memory))
            for (gpu_uuid, username), items in grouped.items():
                gpu = gpus.get(gpu_uuid)
                if gpu is None or gpu[1] > self.utilization_threshold_percent:
                    continue
                used_memory = sum(memory for _, memory in items)
                if used_memory < self.memory_threshold_mb:
                    continue
                candidates.append(
                    GpuIdleCandidate(
                        host=host,
                        gpu_uuid=gpu_uuid,
                        gpu_index=gpu[0],
                        server_username=username,
                        used_memory_mb=used_memory,
                        pids=tuple(sorted(pid for pid, _ in items)),
                    )
                )
        return GpuScan(
            reachable_hosts=tuple(reachable),
            candidates=tuple(
                sorted(
                    candidates,
                    key=lambda item: (item.host, item.gpu_index, item.server_username),
                )
            ),
        )

    def _parse_gpus(self, text: str) -> dict[str, tuple[int, int]]:
        result = {}
        for line in text.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 7 or not parts[1].startswith("GPU-"):
                continue
            index = self._non_negative_integer(parts[0])
            utilization = self._non_negative_integer(parts[6])
            if index is None or utilization is None or utilization > 100:
                continue
            result[parts[1]] = (index, utilization)
        return result

    def _parse_processes(self, text: str) -> list[tuple[str, int, int, str]]:
        result = []
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) != 5 or not parts[0].startswith("GPU-"):
                continue
            pid = self._non_negative_integer(parts[1])
            memory = self._non_negative_integer(parts[2])
            username = parts[3].strip()
            if pid is None or memory is None or not USERNAME_RE.fullmatch(username):
                continue
            result.append((parts[0], pid, memory, username))
        return result


class GpuStatusScanner:
    GPU_QUERY = GpuFleetScanner.GPU_QUERY

    def __init__(
        self,
        hosts: Sequence[str],
        runner: Runner | None = None,
    ) -> None:
        self.hosts = tuple(hosts)
        self.runner = runner or GpuFleetScanner._run_ssh

    def scan(self) -> tuple[ServerGpuStatus, ...]:
        def safe_run(host: str) -> tuple[int, str]:
            try:
                return self.runner(host, self.GPU_QUERY)
            except (OSError, subprocess.SubprocessError, TimeoutError):
                return 255, ""

        with ThreadPoolExecutor(max_workers=max(1, len(self.hosts))) as executor:
            futures = {host: executor.submit(safe_run, host) for host in self.hosts}

        statuses = []
        for host in self.hosts:
            return_code, output = futures[host].result()
            statuses.append(
                ServerGpuStatus(
                    host=host,
                    reachable=return_code == 0,
                    devices=(
                        self._parse_devices(output) if return_code == 0 else ()
                    ),
                )
            )
        return tuple(statuses)

    @staticmethod
    def _parse_devices(text: str) -> tuple[GpuDeviceStatus, ...]:
        devices = []
        for line in text.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 7:
                continue
            numbers = [GpuFleetScanner._non_negative_integer(value) for value in parts]
            index, total, used, utilization = (
                numbers[0],
                numbers[3],
                numbers[4],
                numbers[6],
            )
            if (
                index is None
                or total is None
                or used is None
                or utilization is None
                or utilization > 100
            ):
                continue
            devices.append(
                GpuDeviceStatus(
                    index=index,
                    name=parts[2],
                    total_memory_mb=total,
                    used_memory_mb=used,
                    utilization_percent=utilization,
                )
            )
        return tuple(sorted(devices, key=lambda device: device.index))


def format_gpu_status(statuses: Sequence[ServerGpuStatus]) -> str:
    sections = [
        "📡 <b>Статус GPU прямо сейчас</b>",
        "Загрузка и память обновлены при нажатии кнопки.",
    ]
    for status in statuses:
        server_name = html.escape(PUBLIC_SERVER_NAMES.get(status.host, status.host))
        if not status.reachable:
            sections.append(
                f"🔴 <b>{server_name}</b>\nСервер недоступен по SSH."
            )
            continue
        if not status.devices:
            sections.append(
                f"🟡 <b>{server_name}</b>\nСервер доступен, GPU не найдены."
            )
            continue
        lines = [f"🟢 <b>{server_name}</b>"]
        for device in status.devices:
            utilization_filled = min(
                10, (device.utilization_percent + 9) // 10
            )
            utilization_bar = "🟩" * utilization_filled + "⬜" * (
                10 - utilization_filled
            )
            memory_percent = (
                min(
                    100,
                    round(100 * device.used_memory_mb / device.total_memory_mb),
                )
                if device.total_memory_mb
                else 0
            )
            memory_filled = min(10, (memory_percent + 9) // 10)
            memory_bar = "🟦" * memory_filled + "⬜" * (10 - memory_filled)
            lines.extend(
                [
                    f"GPU {device.index} · {html.escape(device.name)}",
                    (
                        f"<b>UTIL</b>  {utilization_bar} "
                        f"{device.utilization_percent}%"
                    ),
                    (
                        f"<b>VRAM</b> {memory_bar} {memory_percent}% · "
                        f"{device.used_memory_mb / 1024:.1f} / "
                        f"{device.total_memory_mb / 1024:.1f} GB"
                    ),
                ]
            )
        sections.append("\n".join(lines))
    sections.append("Нажмите «📡 Статус серверов» ещё раз, чтобы обновить данные.")
    return "\n\n".join(sections)


class GpuIdleMonitor:
    def __init__(
        self,
        storage: Storage,
        scanner: GpuFleetScanner,
        policy: GpuIdlePolicy,
    ) -> None:
        self.storage = storage
        self.scanner = scanner
        self.policy = policy

    def check(self, *, now: int | None = None) -> list[GpuIdleIncident]:
        checked_at = int(time.time()) if now is None else now
        scan = self.scanner.scan()
        return self.storage.update_gpu_idle_observations(
            scan.candidates,
            reachable_hosts=scan.reachable_hosts,
            now=checked_at,
            minimum_duration_seconds=self.policy.minimum_duration_seconds,
            minimum_samples=self.policy.minimum_samples,
            maximum_sample_gap_seconds=self.policy.interval_seconds * 2,
            notification_cooldown_seconds=(
                self.policy.notification_cooldown_seconds
            ),
        )


def format_user_idle_notification(
    incidents: Sequence[GpuIdleIncident],
    policy: GpuIdlePolicy = GpuIdlePolicy(),
) -> str:
    sections = [
        "⚠️ <b>Похоже, GPU простаивает</b>",
        "Бот не завершает процессы автоматически. Проверьте их и освободите "
        "память, если расчёт уже не нужен.",
    ]
    for incident in incidents:
        server_name = PUBLIC_SERVER_NAMES.get(incident.host, incident.host)
        memory_gb = incident.used_memory_mb / 1024
        pids = ", ".join(str(pid) for pid in incident.pids)
        sections.append(
            f"<b>{html.escape(server_name)}</b> · GPU {incident.gpu_index}\n"
            f"Занято: <b>{memory_gb:.1f} GB</b> VRAM\n"
            f"PID: <code>{pids}</code>\n"
            "Загрузка карты остаётся не выше "
            f"{policy.utilization_threshold_percent}% не менее "
            f"{policy.minimum_duration_seconds // 60} минут."
        )
    sections.append(
        "Если сейчас идёт загрузка данных, evaluation или checkpoint, сообщение "
        "можно проигнорировать."
    )
    return "\n\n".join(sections)


def format_admin_idle_notification(
    incidents: Sequence[GpuIdleIncident],
    *,
    delivery_failed: bool = False,
    policy: GpuIdlePolicy = GpuIdlePolicy(),
) -> str:
    username = html.escape(incidents[0].server_username)
    reason = (
        "пользователь не принимает сообщения от бота"
        if delivery_failed
        else "Unix username ещё не связан с Telegram ID"
    )
    lines = [
        "⚠️ <b>Не удалось уведомить владельца GPU-процесса</b>",
        f"Unix username: <code>{username}</code>",
        f"Причина: {reason}.",
        "",
        "<b>Кому написать</b>",
        "Найдите владельца этого Unix username в общей беседе или таблице "
        "и отправьте ему текст ниже в личные сообщения.",
        "",
        "<b>Готовое сообщение</b>",
    ]
    message_lines = []
    for incident in incidents:
        server_location = SERVER_LOCATIONS.get(incident.host, incident.host)
        message_lines.append(
            f"Привет! На {html.escape(server_location)} процесс на GPU "
            f"{incident.gpu_index} уже не менее "
            f"{policy.minimum_duration_seconds // 60} минут занимает "
            f"{incident.used_memory_mb / 1024:.1f} GB VRAM, а загрузка карты "
            f"остаётся не выше {policy.utilization_threshold_percent}%. "
            f"PID: {', '.join(str(pid) for pid in incident.pids)}."
        )
    message_lines.append(
        "Пожалуйста, проверь, нужен ли ещё этот процесс. Если расчёт завершён "
        "или завис, освободи GPU-память. Если сейчас идёт загрузка данных, "
        "evaluation или сохранение checkpoint, сообщение можно проигнорировать."
    )
    lines.append("<blockquote>" + "\n\n".join(message_lines) + "</blockquote>")
    return "\n".join(lines)


async def deliver_gpu_idle_incidents(
    bot: object,
    storage: Storage,
    incidents: Sequence[GpuIdleIncident],
    *,
    admin_user_ids: frozenset[int],
    now: int,
    policy: GpuIdlePolicy = GpuIdlePolicy(),
    process_controller: GpuProcessController | None = None,
) -> tuple[str, ...]:
    grouped: dict[str, list[GpuIdleIncident]] = defaultdict(list)
    for incident in incidents:
        grouped[incident.server_username].append(incident)
    delivered_keys = []
    for username, user_incidents in grouped.items():
        telegram_user_id = await asyncio.to_thread(
            storage.telegram_user_id_for_server_username, username
        )
        delivery_failed = False
        delivered = False
        if telegram_user_id is not None:
            action_buttons = []
            if process_controller is not None:
                for incident in user_incidents:
                    try:
                        identities = await asyncio.to_thread(
                            process_controller.snapshot,
                            incident.host,
                            incident.server_username,
                            incident.pids,
                        )
                        token = secrets.token_urlsafe(8)
                        await asyncio.to_thread(
                            storage.save_gpu_process_action,
                            GpuProcessAction(
                                token=token,
                                telegram_user_id=telegram_user_id,
                                incident_key=incident.key,
                                host=incident.host,
                                server_username=incident.server_username,
                                identities=identities,
                                status="pending",
                                expires_at=PROCESS_ACTION_NEVER_EXPIRES_AT,
                            ),
                        )
                        server_name = PUBLIC_SERVER_NAMES.get(
                            incident.host, incident.host
                        )
                        action_buttons.append(
                            [
                                InlineKeyboardButton(
                                    text=(
                                        f"🛑 Остановить · {server_name} · "
                                        f"GPU {incident.gpu_index}"
                                    ),
                                    callback_data=f"gpu:stop:{token}",
                                )
                            ]
                        )
                    except ProcessControlError:
                        LOGGER.exception(
                            "Could not prepare GPU process action for %s",
                            incident.key,
                        )
            try:
                send_options = {"protect_content": True}
                if action_buttons:
                    send_options["reply_markup"] = InlineKeyboardMarkup(
                        inline_keyboard=action_buttons
                    )
                await bot.send_message(
                    telegram_user_id,
                    format_user_idle_notification(user_incidents, policy),
                    **send_options,
                )
                delivered = True
            except Exception:
                delivery_failed = True
                LOGGER.exception(
                    "Could not send GPU idle notification to Telegram user %s",
                    telegram_user_id,
                )
        if not delivered:
            admin_text = format_admin_idle_notification(
                user_incidents,
                delivery_failed=delivery_failed,
                policy=policy,
            )
            for admin_user_id in admin_user_ids:
                try:
                    await bot.send_message(
                        admin_user_id, admin_text, protect_content=False
                    )
                    delivered = True
                except Exception:
                    LOGGER.exception(
                        "Could not send GPU idle notification to admin %s",
                        admin_user_id,
                    )
        if delivered:
            delivered_keys.extend(incident.key for incident in user_incidents)
    keys = tuple(delivered_keys)
    if keys:
        await asyncio.to_thread(storage.mark_gpu_idle_notified, keys, now=now)
    return keys


async def run_gpu_idle_monitor(
    bot: object,
    monitor: GpuIdleMonitor,
    *,
    admin_user_ids: frozenset[int],
    process_controller: GpuProcessController | None = None,
) -> None:
    while True:
        started_at = time.monotonic()
        try:
            now = int(time.time())
            incidents = await asyncio.to_thread(monitor.check, now=now)
            if incidents:
                await deliver_gpu_idle_incidents(
                    bot,
                    monitor.storage,
                    incidents,
                    admin_user_ids=admin_user_ids,
                    now=now,
                    policy=monitor.policy,
                    process_controller=process_controller,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("GPU idle monitor iteration failed")
        elapsed = time.monotonic() - started_at
        await asyncio.sleep(max(1, monitor.policy.interval_seconds - elapsed))
