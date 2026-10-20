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
    assert "UpdateHostKeys=no" in calls[0]


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
            "-o",
            "UpdateHostKeys=no",
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


def test_alibaba_provisioner_creates_account_and_parses_credentials() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "[2026-09-04T11:14:33Z] готово: alice (Alice Example)\n"
            "    ssh alice@10.0.117.197\n"
            "    пароль: generated-secret\n"
            "[2026-09-04T11:14:33Z] лаборатория brainlab: счёт brainlab, "
            "партиция brain, карты brain\n",
            "",
        )

    access = SshProvisioner(run=run).provision(
        "alibaba_ascend", "alice", full_name="Alice Example", owner_id=101
    )

    assert access.host == "alibaba_ascend"
    assert access.password == "generated-secret"
    assert access.connection == "ssh alice@10.0.117.197"
    assert access.created is True
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
            "-o",
            "UpdateHostKeys=no",
            "-i",
            "/root/.ssh/china_cluster_agents",
            "-o",
            "IdentitiesOnly=yes",
            "veprikov@10.0.117.197",
            "/home/veprikov/.local/bin/brainlab-bot-alibaba-provision "
            "provision alice 'Alice Example' 101",
        ]
    ]


def test_alibaba_failure_redacts_password_from_error() -> None:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "setup failed\n    пароль: must-not-leak\n",
        )

    try:
        SshProvisioner(run=run).provision(
            "alibaba_ascend", "alice", full_name="Alice Example", owner_id=101
        )
    except Exception as error:
        assert "must-not-leak" not in str(error)
    else:
        raise AssertionError("failed Alibaba provisioning was accepted")


def test_alibaba_failure_reports_helper_verdict_not_ssh_banner() -> None:
    stderr = (
        "Authorized only. All activity will be monitored and reported\n"
        "BOT_OWNER: 101\n"
        "[2026-10-01T16:49:22Z] выдано потолков: 5.8T, просят ещё 200G\n"
        "[2026-10-01T16:49:22Z] ОШИБКА: потолки перестанут быть обеспечены местом.\n"
    )

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", stderr)

    try:
        SshProvisioner(run=run).provision(
            "alibaba_ascend", "alice", full_name="Alice Example", owner_id=101
        )
    except Exception as error:
        assert str(error) == (
            "alibaba_ascend: ОШИБКА: потолки перестанут быть обеспечены местом."
        )
    else:
        raise AssertionError("failed Alibaba provisioning was accepted")


def test_alibaba_username_check_uses_recovery_aware_helper() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    assert SshProvisioner(run=run).username_available("alibaba_ascend", "alice")
    assert calls[0][-1] == (
        "/home/veprikov/.local/bin/brainlab-bot-alibaba-provision available alice"
    )


def test_two_a100_uses_the_standard_add_user_helper() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "HERMES_STATUS=CREATED\nUsername: alice\nPassword: secret\n"
            "Подключение: ssh alice@10.55.230.46\n"
            "Ссылка на тг группу сервера: https://t.me/example",
            "",
        )

    access = SshProvisioner(run=run).provision("2_a100_brain", "alice")

    assert access.host == "2_a100_brain"
    assert access.connection == "ssh alice@10.55.230.46"
    assert calls[0][-5:] == [
        "2_a100_brain",
        "sudo",
        "-n",
        "/usr/local/sbin/hermes-add-user",
        "alice",
    ]
