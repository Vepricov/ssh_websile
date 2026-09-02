import subprocess

from server_access_bot.provisioning import SshProvisioner


def test_username_check_disables_ssh_multiplexing() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 2, "", "")

    assert SshProvisioner(run=run).username_available("shkodnik_opt", "alice")
    assert "ControlMaster=no" in calls[0]
    assert "ControlPath=none" in calls[0]


def test_provisioner_parses_root_helper_credentials() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "HERMES_STATUS=CREATED\nUsername: alice\nPassword: secret\nПодключение: ssh alice@example.org\nСсылка на тг группу сервера: https://t.me/example",
            "",
        )

    access = SshProvisioner(run=run).provision("brain_lab", "alice")

    assert access.password == "secret"
    assert access.connection == "ssh alice@example.org"
    assert calls == [
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
            "brain_lab",
            "sudo",
            "-n",
            "/usr/local/sbin/hermes-add-user",
            "alice",
        ]
    ]


def test_mlsub_uses_its_own_brain_lab_helper_without_a_new_password() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "HERMES_STATUS=CREATED\n"
            "Username: alice\n"
            "Подключение: ssh -p 10210 alice@proxy2.cod.phystech.edu\n"
            "Ссылка на тг группу сервера: https://t.me/example",
            "",
        )

    access = SshProvisioner(run=run).provision("mlsub", "alice")

    assert access.host == "mlsub"
    assert access.password is None
    assert calls[0][-4:] == [
        "sudo",
        "-n",
        "/usr/local/sbin/hermes-enable-mlsub",
        "alice",
    ]
