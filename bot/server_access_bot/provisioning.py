import re
import shlex
import subprocess
from collections.abc import Callable

from .domain import ProvisionedAccess


class ProvisioningError(RuntimeError):
    pass


HOSTS = frozenset(
    {
        "shkodnik_opt",
        "brain_lab",
        "vv_h200",
        "2_a100_brain",
        "mlsub",
        "alibaba_ascend",
        "ipavlov",
    }
)
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
ALIBABA_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
ALIBABA_TARGET = "veprikov@10.0.117.197"
ALIBABA_IDENTITY_FILE = "/root/.ssh/china_cluster_agents"
ALIBABA_HELPER = "/home/veprikov/.local/bin/brainlab-bot-alibaba-provision"
# ipavlov / общий пул: one FreeIPA account (group brainlab) that already has
# HBAC access to both the VV node (ext1) and the gpu nodes. The bot reaches the
# helper over the `gpu12` ssh alias; the FreeIPA admin password is fed on stdin.
IPAVLOV_SSH_HOST = "gpu12"
IPAVLOV_HELPER = "/home/andrey_veprikov/.local/bin/brainlab-ipa"
IPAVLOV_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
IPAVLOV_CONNECTION = "ssh -J {user}@10.0.116.11:30018 {user}@gpu12.ipavlov.mipt.ru"
IPAVLOV_PASSWORD_RE = re.compile(r"(?m)^PASSWORD:\s*(.+)$")
PASSWORD_LINE_RE = re.compile(r"(?im)^(\s*пароль:)\s*.*$")
TIMESTAMP_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
    "-o",
    "UpdateHostKeys=no",
)


def _helper_failure(stderr: str, returncode: int) -> str:
    """The SSH banner comes first in stderr; the helper's own verdict is what matters."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    errors = [
        TIMESTAMP_PREFIX_RE.sub("", line)
        for line in lines
        if "ОШИБКА" in line or line.startswith("ERROR:")
    ]
    if errors:
        return "\n".join(errors)
    return lines[-1] if lines else f"exit code {returncode}"


class SshProvisioner:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        *,
        ipa_password: str | None = None,
    ) -> None:
        self.run = run
        self.ipa_password = ipa_password

    @staticmethod
    def _username_re(host: str) -> re.Pattern[str]:
        if host == "alibaba_ascend":
            return ALIBABA_USERNAME_RE
        if host == "ipavlov":
            return IPAVLOV_USERNAME_RE
        return USERNAME_RE

    @staticmethod
    def _target(host: str) -> list[str]:
        if host == "alibaba_ascend":
            return [
                "-i",
                ALIBABA_IDENTITY_FILE,
                "-o",
                "IdentitiesOnly=yes",
                ALIBABA_TARGET,
            ]
        if host == "ipavlov":
            return [IPAVLOV_SSH_HOST]
        return ["brain_lab" if host == "mlsub" else host]

    def _run_ipavlov(self, arguments: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        if not self.ipa_password:
            raise ProvisioningError("IPA password not configured")
        return self.run(
            [
                "ssh",
                *SSH_OPTIONS,
                *self._target("ipavlov"),
                shlex.join([IPAVLOV_HELPER, *arguments]),
            ],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            input=f"{self.ipa_password}\n",
        )

    @staticmethod
    def _ipavlov_password(stdout: str) -> str:
        match = IPAVLOV_PASSWORD_RE.search(stdout)
        if not match:
            raise ProvisioningError(
                "ipavlov: provisioning helper omitted the password"
            )
        return match.group(1)

    def reset_ipavlov_password(self, username: str) -> str:
        if not IPAVLOV_USERNAME_RE.fullmatch(username):
            raise ProvisioningError("invalid ipavlov username")
        completed = self._run_ipavlov(["reset", username], timeout=60)
        if completed.returncode != 0:
            raise ProvisioningError(
                f"ipavlov: {_helper_failure(completed.stderr, completed.returncode)}"
            )
        return self._ipavlov_password(completed.stdout)

    def username_available(self, host: str, username: str) -> bool:
        username_re = self._username_re(host)
        if host not in HOSTS or not username_re.fullmatch(username):
            raise ProvisioningError("invalid provisioning target")
        if host == "mlsub":
            return True
        if host == "ipavlov":
            completed = self._run_ipavlov(["available", username], timeout=30)
            if completed.returncode == 0:
                return True
            if completed.returncode == 1:
                return False
            raise ProvisioningError(
                "ipavlov: username check failed: "
                f"{_helper_failure(completed.stderr, completed.returncode)}"
            )
        remote_command = ["getent", "passwd", "--", username]
        if host == "alibaba_ascend":
            remote_command = [ALIBABA_HELPER, "available", username]
        completed = self.run(
            [
                "ssh",
                *SSH_OPTIONS,
                *self._target(host),
                shlex.join(remote_command),
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if host == "alibaba_ascend":
            if completed.returncode == 0:
                return True
            if completed.returncode == 1:
                return False
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise ProvisioningError(f"{host}: username check failed: {detail}")
        if completed.returncode == 0:
            return False
        if completed.returncode in (1, 2):
            return True
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise ProvisioningError(f"{host}: username check failed: {detail}")

    def provision(
        self,
        host: str,
        username: str,
        *,
        full_name: str | None = None,
        owner_id: int | None = None,
    ) -> ProvisionedAccess:
        username_re = self._username_re(host)
        if host not in HOSTS or not username_re.fullmatch(username):
            raise ProvisioningError("invalid provisioning target")
        if host == "ipavlov":
            if (
                not full_name
                or len(full_name) > 64
                or any(character in full_name for character in "\r\n\0")
            ):
                raise ProvisioningError("invalid ipavlov application")
            completed = self._run_ipavlov(["add", username, full_name], timeout=120)
            if completed.returncode != 0:
                raise ProvisioningError(
                    f"ipavlov: {_helper_failure(completed.stderr, completed.returncode)}"
                )
            return ProvisionedAccess(
                host="ipavlov",
                username=username,
                password=self._ipavlov_password(completed.stdout),
                connection=IPAVLOV_CONNECTION.format(user=username),
                group_url="",
                created=True,
            )
        if host == "alibaba_ascend":
            if (
                not full_name
                or len(full_name) > 64
                or any(character in full_name for character in "\r\n\0")
                or owner_id is None
                or owner_id <= 0
            ):
                raise ProvisioningError("invalid Alibaba application")
            command = [
                "ssh",
                *SSH_OPTIONS,
                *self._target(host),
                shlex.join(
                    [ALIBABA_HELPER, "provision", username, full_name, str(owner_id)]
                ),
            ]
            return self._provision_alibaba(command, username)
        target_host = "brain_lab" if host == "mlsub" else host
        helper = (
            "/usr/local/sbin/hermes-enable-mlsub"
            if host == "mlsub"
            else "/usr/local/sbin/hermes-add-user"
        )
        command = [
            "ssh",
            *SSH_OPTIONS,
            target_host,
            "sudo",
            "-n",
            helper,
            username,
        ]
        completed = self.run(
            command,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise ProvisioningError(f"{host}: {detail}")
        lines = completed.stdout.strip().splitlines()
        if not lines or lines[0] not in {
            "HERMES_STATUS=CREATED",
            "HERMES_STATUS=EXISTS",
        }:
            raise ProvisioningError(
                f"{host}: provisioning helper returned an invalid status"
            )
        fields: dict[str, str] = {}
        for line in lines[1:]:
            key, separator, value = line.partition(": ")
            if separator:
                fields[key] = value
        created = lines[0] == "HERMES_STATUS=CREATED"
        required = ["Username", "Подключение", "Ссылка на тг группу сервера"]
        if host != "mlsub":
            required.append("Password")
        if created and not all(fields.get(key) for key in required):
            raise ProvisioningError(f"{host}: provisioning helper omitted credentials")
        return ProvisionedAccess(
            host=host,
            username=fields.get("Username", username),
            password=fields.get("Password"),
            connection=fields.get("Подключение", ""),
            group_url=fields.get("Ссылка на тг группу сервера", ""),
            created=created,
        )

    def _provision_alibaba(
        self, command: list[str], username: str
    ) -> ProvisionedAccess:
        completed = self.run(
            command,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            stderr = PASSWORD_LINE_RE.sub(r"\1 [REDACTED]", completed.stderr)
            raise ProvisioningError(
                f"alibaba_ascend: {_helper_failure(stderr, completed.returncode)}"
            )
        password = ""
        connection = ""
        created = False
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("пароль:"):
                password = line.partition(":")[2].strip()
            elif line == f"ssh {username}@10.0.117.197":
                connection = line
            elif f"готово: {username} (" in line:
                created = True
        if not created or not password or not connection:
            raise ProvisioningError(
                "alibaba_ascend: provisioning helper omitted credentials"
            )
        return ProvisionedAccess(
            host="alibaba_ascend",
            username=username,
            password=password,
            connection=connection,
            group_url="",
            created=True,
        )

    def acknowledge(self, host: str, username: str, *, owner_id: int) -> None:
        if host != "alibaba_ascend" or not ALIBABA_USERNAME_RE.fullmatch(username):
            return
        completed = self.run(
            [
                "ssh",
                *SSH_OPTIONS,
                *self._target(host),
                shlex.join(
                    [ALIBABA_HELPER, "acknowledge", username, str(owner_id)]
                ),
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit code {completed.returncode}"
            raise ProvisioningError(f"{host}: recovery cleanup failed: {detail}")
