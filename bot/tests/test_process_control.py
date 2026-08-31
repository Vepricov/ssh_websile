import subprocess

import pytest

from server_access_bot.domain import GpuProcessAction, ProcessIdentity
from server_access_bot.process_control import (
    GpuProcessController,
    ProcessControlError,
    signal_gpu_process_action,
)
from server_access_bot.storage import Storage


def test_controller_snapshots_and_signals_exact_process_identity() -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "inspect" in command:
            return subprocess.CompletedProcess(command, 0, "303:987654\n", "")
        return subprocess.CompletedProcess(command, 0, "STATUS=SIGTERM_SENT\n", "")

    controller = GpuProcessController(
        run=run, sudo_passwords={"brain_lab": "sudo-secret"}
    )

    identities = controller.snapshot("brain_lab", "alice", (303,))
    controller.signal("brain_lab", "alice", identities, "TERM")

    assert identities == (ProcessIdentity(pid=303, start_ticks=987654),)
    assert calls[0][0][-3:] == ["inspect", "alice", "303"]
    assert calls[1][0][-3:] == ["term", "alice", "303:987654"]
    assert all(kwargs["shell"] is False for _, kwargs in calls)
    assert all("sudo-secret" not in command for command, _ in calls)
    assert all(kwargs["input"] == "sudo-secret\n" for _, kwargs in calls)


def test_controller_uses_non_interactive_sudo_for_yandex() -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "303:987654\n", "")

    controller = GpuProcessController(run=run, nopasswd_hosts={"yandex"})

    identities = controller.snapshot("yandex", "alice", (303,))

    assert identities == (ProcessIdentity(pid=303, start_ticks=987654),)
    assert calls[0][0][-6:] == [
        "sudo",
        "-n",
        "/usr/local/sbin/hermes-gpu-process-control",
        "inspect",
        "alice",
        "303",
    ]
    assert calls[0][1]["input"] is None


def test_controller_rejects_untrusted_targets_before_ssh() -> None:
    controller = GpuProcessController(sudo_passwords={"brain_lab": "secret"})

    with pytest.raises(ProcessControlError):
        controller.snapshot("other-host", "alice", (303,))
    with pytest.raises(ProcessControlError):
        controller.snapshot("brain_lab", "alice;id", (303,))
    with pytest.raises(ProcessControlError):
        controller.signal(
            "brain_lab",
            "alice",
            (ProcessIdentity(pid=303, start_ticks=1),),
            "HUP",
        )


def test_alive_ignores_gone_or_pid_reused_processes() -> None:
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "303:999999\n", "")

    controller = GpuProcessController(
        run=run, sudo_passwords={"brain_lab": "sudo-secret"}
    )
    alive = controller.alive(
        "brain_lab",
        "alice",
        (ProcessIdentity(303, 987654), ProcessIdentity(404, 123456)),
    )

    assert alive == ()


def test_process_action_is_owner_bound_expiring_and_single_use(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    action = GpuProcessAction(
        token="safe-token",
        telegram_user_id=101,
        incident_key="brain_lab:GPU-aaaa:alice",
        host="brain_lab",
        server_username="alice",
        identities=(ProcessIdentity(303, 987654),),
        status="pending",
        expires_at=200,
    )
    storage.save_gpu_process_action(action)

    assert storage.gpu_process_action_for("safe-token", 202, now=100) is None
    assert storage.gpu_process_action_for("safe-token", 101, now=201) is None

    claimed = storage.transition_gpu_process_action(
        "safe-token", 101, "pending", "term_sending", now=100
    )
    assert claimed == action
    assert (
        storage.transition_gpu_process_action(
            "safe-token", 101, "pending", "term_sending", now=100
        )
        is None
    )


def test_term_then_kill_requires_delay_and_each_signal_is_single_use(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    action = GpuProcessAction(
        token="safe-token",
        telegram_user_id=101,
        incident_key="brain_lab:GPU-aaaa:alice",
        host="brain_lab",
        server_username="alice",
        identities=(ProcessIdentity(303, 987654),),
        status="pending",
        expires_at=200,
    )
    storage.save_gpu_process_action(action)

    class FakeController:
        def __init__(self):
            self.calls = []

        def signal(self, host, username, identities, signal_name):
            self.calls.append((host, username, identities, signal_name))

    controller = FakeController()
    term = signal_gpu_process_action(
        storage, controller, "safe-token", 101, "TERM", now=100
    )
    assert term.status == "term_sent"
    assert term.term_requested_at == 100
    assert storage.gpu_process_action_for("safe-token", 101, now=10**12) is not None

    with pytest.raises(ProcessControlError):
        signal_gpu_process_action(
            storage, controller, "safe-token", 101, "TERM", now=101
        )
    with pytest.raises(ProcessControlError):
        signal_gpu_process_action(
            storage, controller, "safe-token", 101, "KILL", now=109
        )

    killed = signal_gpu_process_action(
        storage, controller, "safe-token", 101, "KILL", now=110
    )
    assert killed.status == "completed"
    assert [call[-1] for call in controller.calls] == ["TERM", "KILL"]


def test_failed_signal_returns_action_to_retryable_state(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_gpu_process_action(
        GpuProcessAction(
            token="safe-token",
            telegram_user_id=101,
            incident_key="brain_lab:GPU-aaaa:alice",
            host="brain_lab",
            server_username="alice",
            identities=(ProcessIdentity(303, 987654),),
            status="pending",
            expires_at=200,
        )
    )

    class FailingController:
        def signal(self, host, username, identities, signal_name):
            raise ProcessControlError("network failed")

    with pytest.raises(ProcessControlError):
        signal_gpu_process_action(
            storage, FailingController(), "safe-token", 101, "TERM", now=100
        )

    action = storage.gpu_process_action_for("safe-token", 101, now=100)
    assert action is not None
    assert action.status == "pending"
