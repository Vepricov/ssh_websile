import re
import subprocess
from collections.abc import Callable

from .domain import ProvisionedAccess


class ProvisioningError(RuntimeError):
    pass


HOSTS = frozenset({"shkodnik_opt", "brain_lab", "vv_h200", "mlsub"})
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ControlMaster=no",
    "-o",
    "ControlPath=none",
)


class SshProvisioner:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.run = run

    def username_available(self, host: str, username: str) -> bool:
        if host not in HOSTS or not USERNAME_RE.fullmatch(username):
            raise ProvisioningError("invalid provisioning target")
        if host == "mlsub":
            return True
        completed = self.run(
            [
                "ssh",
                *SSH_OPTIONS,
                host,
                "getent",
                "passwd",
                "--",
                username,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0:
            return False
        if completed.returncode in (1, 2):
            return True
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise ProvisioningError(f"{host}: username check failed: {detail}")

    def provision(self, host: str, username: str) -> ProvisionedAccess:
        if host not in HOSTS or not USERNAME_RE.fullmatch(username):
            raise ProvisioningError("invalid provisioning target")
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
