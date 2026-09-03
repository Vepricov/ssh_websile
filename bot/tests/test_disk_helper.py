import importlib.util
import json
import os
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


def test_zfs_filesystem_usage_includes_child_datasets(monkeypatch) -> None:
    helper = load_helper()

    def fake_run(command, timeout=30):
        if command[:2] == ["zfs", "list"]:
            return subprocess.CompletedProcess(command, 0, "900000\t100000\n", "")
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(helper, "run", fake_run)

    assert helper.zfs_filesystem_usage("tank/home") == {
        "used_bytes": 900000,
        "available_bytes": 100000,
        "total_bytes": 1000000,
    }


def test_top_home_cache_reads_completed_fast_scan(tmp_path: Path) -> None:
    helper = load_helper()
    cache = tmp_path / "latest.tsv"
    cache.write_text(
        "120000\t/home\n"
        "70000\t/home/alice\n"
        "90000\t/home/bob\n"
        "invalid\n"
        "50000\t/home/nested/path\n",
        encoding="utf-8",
    )

    assert helper.top_home_cache(
        str(cache), now=int(cache.stat().st_mtime), expected_uid=os.getuid()
    ) == [
        {"path": "/home/bob", "size_bytes": 90000},
        {"path": "/home/alice", "size_bytes": 70000},
    ]


def test_top_home_cache_rejects_stale_file(tmp_path: Path) -> None:
    helper = load_helper()
    cache = tmp_path / "latest.tsv"
    cache.write_text("70000\t/home/alice\n", encoding="utf-8")

    assert helper.top_home_cache(
        str(cache),
        now=int(cache.stat().st_mtime) + 48 * 3600 + 1,
        expected_uid=os.getuid(),
    ) is None


def test_top_home_cache_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    helper = load_helper()
    cache = tmp_path / "snapshot.tsv"
    cache.write_text("70000\t/home/alice\n", encoding="utf-8")
    link = tmp_path / "latest.tsv"
    link.symlink_to(cache.name)
    now = int(cache.stat().st_mtime)

    assert helper.top_home_cache(
        str(link), now=now, expected_uid=os.getuid()
    ) is None
    assert helper.top_home_cache(
        str(cache), now=now, expected_uid=os.getuid(), max_bytes=1
    ) is None


def test_top_home_falls_back_to_completed_cache(monkeypatch, capsys) -> None:
    helper = load_helper()
    monkeypatch.setattr(helper.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(
        helper,
        "run",
        lambda command, timeout=30: subprocess.CompletedProcess(
            command, 0, "ext4 /dev/sdb\n", ""
        ),
    )
    monkeypatch.setattr(helper, "top_home_quota", lambda: [])
    monkeypatch.setattr(
        helper,
        "top_home_cache",
        lambda: [{"path": "/home/alice", "size_bytes": 70000}],
    )

    helper.top_home()

    assert json.loads(capsys.readouterr().out) == {
        "directories": [{"path": "/home/alice", "size_bytes": 70000}],
        "source": "cache",
    }


def test_top_home_returns_ten_largest_directories(monkeypatch, capsys) -> None:
    helper = load_helper()
    rows = [
        {"path": f"/home/user{index}", "size_bytes": index}
        for index in range(12)
    ]
    monkeypatch.setattr(helper.os.path, "isdir", lambda path: True)
    monkeypatch.setattr(
        helper,
        "run",
        lambda command, timeout=30: subprocess.CompletedProcess(
            command, 0, "ext4 /dev/sdb\n", ""
        ),
    )
    monkeypatch.setattr(helper, "top_home_quota", lambda: rows)

    helper.top_home()

    payload = json.loads(capsys.readouterr().out)
    assert len(payload["directories"]) == 10
    assert payload["directories"][0]["path"] == "/home/user11"
    assert payload["directories"][-1]["path"] == "/home/user2"


def test_home_scan_timer_is_low_priority_and_bounded() -> None:
    deploy = Path(__file__).parents[1] / "deploy"
    scanner = (deploy / "hermes-home-scan").read_text(encoding="utf-8")
    fast_du = (deploy / "hermes-fast-du").read_text(encoding="utf-8")
    service = (deploy / "hermes-home-scan.service").read_text(encoding="utf-8")
    timer = (deploy / "hermes-home-scan.timer").read_text(encoding="utf-8")

    assert "/usr/local/sbin/hermes-fast-du /home" in scanner
    assert "os.replace(temporary, destination)" in scanner
    assert "entry.is_dir(follow_symlinks=False)" in fast_du
    assert "os.fsync(output.fileno())" in fast_du
    assert "ExecStart=/usr/local/sbin/hermes-home-scan" in service
    assert "Nice=19" in service
    assert "IOSchedulingClass=idle" in service
    assert "TimeoutStartSec=45min" in service
    assert "OnCalendar=*-*-* 03:30:00" in timer
    assert "RandomizedDelaySec=30min" in timer
