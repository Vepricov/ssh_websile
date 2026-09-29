import re
import subprocess
from collections.abc import Callable, Mapping, Sequence

from .domain import GpuProcessAction, ProcessIdentity
from .storage import Storage

HOSTS = frozenset(
    {"shkodnik_opt", "brain_lab", "vv_h200", "2_a100_brain", "yandex"}
)
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HELPER = "/usr/local/sbin/hermes-gpu-process-control"
PROCESS_ACTION_NEVER_EXPIRES_AT = 2**63 - 1


class ProcessControlError(RuntimeError):
    pass


class GpuProcessController:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sudo_passwords: Mapping[str, str] | None = None,
        nopasswd_hosts: Sequence[str] = (),
    ) -> None:
        self.run = run
        self.sudo_passwords = dict(sudo_passwords or {})
        self.nopasswd_hosts = frozenset(nopasswd_hosts)

    @staticmethod
    def _validate_target(host: str, username: str) -> None:
        if host not in HOSTS or not USERNAME_RE.fullmatch(username):
            raise ProcessControlError("invalid process-control target")

    @staticmethod
    def _validate_pids(pids: Sequence[int]) -> tuple[int, ...]:
        normalized = tuple(sorted(set(pids)))
        if not normalized or any(pid <= 1 or pid > 2_147_483_647 for pid in normalized):
            raise ProcessControlError("invalid PID list")
        return normalized

    def _run(self, host: str, arguments: Sequence[str]) -> str:
        password = self.sudo_passwords.get(host)
        uses_nopasswd = host in self.nopasswd_hosts
        if password is None and not uses_nopasswd:
            raise ProcessControlError(f"{host}: sudo credential is unavailable")
        sudo_arguments = ["sudo", "-n"] if uses_nopasswd else ["sudo", "-S"]
        try:
            completed = self.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "ControlMaster=no",
                    "-o",
                    "ControlPath=none",
                    "--",
                    host,
                    *sudo_arguments,
                    HELPER,
                    *arguments,
                ],
                input=None if uses_nopasswd else password + "\n",
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProcessControlError(f"{host}: process-control SSH failed") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise ProcessControlError(f"{host}: {detail}")
        return completed.stdout

    def snapshot(
        self, host: str, username: str, pids: Sequence[int]
    ) -> tuple[ProcessIdentity, ...]:
        return self._inspect(host, username, pids, require_all=True)

    def _inspect(
        self,
        host: str,
        username: str,
        pids: Sequence[int],
        *,
        require_all: bool,
    ) -> tuple[ProcessIdentity, ...]:
        self._validate_target(host, username)
        normalized = self._validate_pids(pids)
        output = self._run(
            host,
            ("inspect", username, *(str(pid) for pid in normalized)),
        )
        identities = []
        for line in output.splitlines():
            pid_text, separator, ticks_text = line.partition(":")
            if not separator or not pid_text.isdigit() or not ticks_text.isdigit():
                raise ProcessControlError(f"{host}: helper returned invalid identity")
            identities.append(ProcessIdentity(int(pid_text), int(ticks_text)))
        result = tuple(sorted(identities))
        returned_pids = tuple(identity.pid for identity in result)
        if any(pid not in normalized for pid in returned_pids) or (
            require_all and returned_pids != normalized
        ):
            raise ProcessControlError(f"{host}: process set changed")
        return result

    def alive(
        self,
        host: str,
        username: str,
        identities: Sequence[ProcessIdentity],
    ) -> tuple[ProcessIdentity, ...]:
        expected = tuple(sorted(identities))
        current = self._inspect(
            host,
            username,
            tuple(item.pid for item in expected),
            require_all=False,
        )
        return tuple(item for item in current if item in expected)

    def signal(
        self,
        host: str,
        username: str,
        identities: Sequence[ProcessIdentity],
        signal_name: str,
    ) -> None:
        self._validate_target(host, username)
        if signal_name not in {"TERM", "KILL"}:
            raise ProcessControlError("invalid signal")
        normalized = tuple(sorted(set(identities)))
        self._validate_pids(tuple(item.pid for item in normalized))
        if any(item.start_ticks <= 0 for item in normalized):
            raise ProcessControlError("invalid process start time")
        output = self._run(
            host,
            (
                signal_name.lower(),
                username,
                *(f"{item.pid}:{item.start_ticks}" for item in normalized),
            ),
        )
        if output.strip() != f"STATUS=SIG{signal_name}_SENT":
            raise ProcessControlError(f"{host}: helper returned invalid status")


def signal_gpu_process_action(
    storage: Storage,
    controller: GpuProcessController,
    token: str,
    telegram_user_id: int,
    signal_name: str,
    *,
    now: int,
) -> GpuProcessAction:
    if signal_name == "TERM":
        expected_status = "pending"
        sending_status = "term_sending"
        final_status = "term_sent"
    elif signal_name == "KILL":
        expected_status = "term_sent"
        sending_status = "kill_sending"
        final_status = "completed"
    else:
        raise ProcessControlError("invalid signal")
    current = storage.gpu_process_action_for(token, telegram_user_id, now=now)
    if current is None or current.status != expected_status:
        raise ProcessControlError("process action is unavailable")
    if signal_name == "KILL" and (
        current.term_requested_at is None or now - current.term_requested_at < 10
    ):
        raise ProcessControlError("force kill is not available yet")
    claimed = storage.transition_gpu_process_action(
        token,
        telegram_user_id,
        expected_status,
        sending_status,
        now=now,
    )
    if claimed is None:
        raise ProcessControlError("process action was already handled")
    try:
        controller.signal(
            claimed.host,
            claimed.server_username,
            claimed.identities,
            signal_name,
        )
    except ProcessControlError:
        storage.transition_gpu_process_action(
            token,
            telegram_user_id,
            sending_status,
            expected_status,
            now=now,
        )
        raise
    storage.transition_gpu_process_action(
        token,
        telegram_user_id,
        sending_status,
        final_status,
        now=now,
        expires_at=PROCESS_ACTION_NEVER_EXPIRES_AT,
        term_requested_at=now if signal_name == "TERM" else None,
    )
    storage.record_event(
        telegram_user_id,
        f"gpu_process_sig{signal_name.lower()}",
        claimed.host,
        "pids=" + ",".join(str(item.pid) for item in claimed.identities),
    )
    result = storage.gpu_process_action_for(token, telegram_user_id, now=now)
    if result is None:
        raise ProcessControlError("process action result is unavailable")
    return result
