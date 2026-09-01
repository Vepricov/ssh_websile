import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path


def load_helper():
    path = Path(__file__).parents[1] / "deploy" / "hermes-disk-report"
    loader = SourceFileLoader("hermes_disk_report", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_cleanup_step_reports_timeout(monkeypatch) -> None:
    helper = load_helper()
    monkeypatch.setattr(helper, "available_bytes", lambda path: 10)

    def timeout(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(helper, "run", timeout)

    assert helper.cleanup_step("Кеш apt", ["apt-get", "clean"], 30) == {
        "name": "Кеш apt",
        "freed_bytes": 0,
        "status": "timeout",
    }


def test_cleanup_root_reports_busy_without_waiting(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    helper = load_helper()
    monkeypatch.setattr(helper, "available_bytes", lambda path: 10)

    def busy(lock, flags):
        raise BlockingIOError

    monkeypatch.setattr(helper.fcntl, "flock", busy)

    helper.cleanup_root(str(tmp_path / "cleanup.lock"))

    payload = json.loads(capsys.readouterr().out)
    assert payload["before_available_bytes"] == 10
    assert payload["after_available_bytes"] == 10
    assert payload["actions"] == [
        {
            "name": "Безопасная очистка",
            "freed_bytes": 0,
            "status": "busy",
        }
    ]
