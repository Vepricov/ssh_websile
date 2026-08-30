import subprocess

from server_access_bot.provisioning import SshProvisioner


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
            "brain_lab",
            "sudo",
            "-n",
            "/usr/local/sbin/hermes-add-user",
            "alice",
        ]
    ]
