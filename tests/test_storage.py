"""Persistence: atomic writes, .bak fallback, interrupted writes, corrupt
files, absolute paths and the single-instance locks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import B, ROOT
import storage


def test_write_then_read_round_trip(tmp_path):
    target = tmp_path / "jobs.json"
    storage.write_json(target, [{"id": "ZM-0001", "username": "ünïcode_é"}])
    assert storage.read_json(target, []) == [{"id": "ZM-0001", "username": "ünïcode_é"}]
    assert not list(tmp_path.glob("*.tmp"))


def test_backup_holds_the_previous_version(tmp_path):
    target = tmp_path / "jobs.json"
    storage.write_json(target, [1])
    storage.write_json(target, [1, 2])
    assert json.loads(storage.backup_path(target).read_text()) == [1]
    assert json.loads(target.read_text()) == [1, 2]


def test_interrupted_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    """Simulate the process dying at the moment of the swap."""
    target = tmp_path / "jobs.json"
    storage.write_json(target, [{"id": "keep-me"}])

    real_replace = os.replace

    def crash_on_primary(src, dst):
        if Path(dst) == target:
            raise OSError("power cut")
        return real_replace(src, dst)
    monkeypatch.setattr(storage.os, "replace", crash_on_primary)

    with pytest.raises(storage.PersistenceError):
        storage.write_json(target, [{"id": "new"}])
    assert json.loads(target.read_text()) == [{"id": "keep-me"}]
    assert not list(tmp_path.glob("*.tmp"))


def test_crash_while_writing_the_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "jobs.json"
    storage.write_json(target, ["safe"])

    def broken_fsync(fd):
        raise OSError("disk full")
    monkeypatch.setattr(storage.os, "fsync", broken_fsync)
    with pytest.raises(storage.PersistenceError):
        storage.write_json(target, ["lost"])
    assert json.loads(target.read_text()) == ["safe"]


def test_unserialisable_data_never_touches_the_disk(tmp_path):
    target = tmp_path / "jobs.json"
    storage.write_json(target, ["safe"])
    with pytest.raises(storage.PersistenceError):
        storage.write_json(target, [object()])
    assert json.loads(target.read_text()) == ["safe"]


@pytest.mark.parametrize("damage", ["", "{\"truncated\": ", "\x00\x00\x00", "{}"])
def test_damaged_primary_falls_back_to_backup_and_is_kept(tmp_path, damage):
    target = tmp_path / "jobs.json"
    storage.write_json(target, [{"id": "v1"}])
    storage.write_json(target, [{"id": "v1"}, {"id": "v2"}])
    target.write_text(damage)                     # e.g. the old truncation bug

    data = storage.read_json(target, [], validate=lambda d: isinstance(d, list))
    assert data == [{"id": "v1"}]                 # the backup
    quarantined = list(tmp_path.glob("jobs.corrupt-*.json"))
    assert len(quarantined) == 1 and quarantined[0].read_text() == damage

    # The next save must not rotate the damaged file over the good backup.
    storage.write_json(target, data)
    assert json.loads(storage.backup_path(target).read_text()) == [{"id": "v1"}]


def test_both_copies_damaged_starts_empty_but_keeps_the_evidence(tmp_path):
    target = tmp_path / "jobs.json"
    target.write_text("not json")
    storage.backup_path(target).write_text("also not json")
    assert storage.read_json(target, []) == []
    assert list(tmp_path.glob("jobs.corrupt-*.json"))


def test_missing_files_give_the_default(tmp_path):
    assert storage.read_json(tmp_path / "nope.json", {"x": 1}) == {"x": 1}


def test_bot_save_failure_is_reported_and_retried(env, monkeypatch, logs):
    env.job("ZM-0001", "abc")
    calls = {"n": 0}
    real = storage.write_json

    def flaky(path, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise storage.PersistenceError("disk full")
        return real(path, payload)
    monkeypatch.setattr(B.storage, "write_json", flaky)
    B.jobs[0]["status"] = "completed"
    assert B.save_jobs() is False and B._save_pending
    B.flush_pending_saves()
    assert not B._save_pending
    assert env.disk_jobs()[0]["status"] == "completed"
    assert any("now gone through" in line for line in logs)


def test_wrong_shaped_config_is_ignored(env):
    B.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    B.CONFIG_FILE.write_text(json.dumps(["not", "a", "dict"]))
    B.load_state()
    assert B.config == {}


def test_config_junk_keys_are_dropped(env):
    B.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    B.CONFIG_FILE.write_text(json.dumps({"None": {"monitor_channel_id": 1},
                                         "123": {"monitor_channel_id": 5}}))
    B.load_state()
    assert list(B.config) == ["123"]


def test_paths_are_absolute_and_relative_to_the_project(monkeypatch):
    monkeypatch.delenv("ZM_DATA_DIR", raising=False)
    for path in (B.BASE_DIR, B.DATA_DIR, B.JOBS_FILE, B.CONFIG_FILE, B.LOG_FILE, B.LOCK_FILE):
        assert Path(path).is_absolute()
    assert B._folder("ZM_DATA_DIR", "data") == ROOT / "data"
    assert B._folder("ZM_LOG_DIR", "logs") / "bot.log" == ROOT / "logs" / "bot.log"
    monkeypatch.setenv("ZM_DATA_DIR", "elsewhere")
    assert B._folder("ZM_DATA_DIR", "data") == ROOT / "elsewhere"   # not the cwd


def test_paths_do_not_depend_on_the_working_directory(tmp_path):
    code = "import bot, sys; print(bot.JOBS_FILE)"
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(ROOT / "data" / "jobs.json")


def test_importing_the_bot_writes_nothing(tmp_path):
    """Tests and tools import bot.py; that must never touch real data/logs."""
    code = ("import os, bot; "
            "print(os.path.exists(bot.DATA_DIR), os.path.exists(bot.LOG_DIR), "
            "len(bot.logging.getLogger('zm').handlers))")
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True,
                         text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr
    # data/ and logs/ may exist from a real run; what matters is no handler.
    assert out.stdout.strip().endswith("0")


def test_data_lock_blocks_a_second_holder(tmp_path):
    first = storage.DataLock(tmp_path / "bot.lock")
    second = storage.DataLock(tmp_path / "bot.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_data_lock_across_processes(tmp_path):
    lock = storage.DataLock(tmp_path / "bot.lock")
    assert lock.acquire()
    code = ("import storage, sys, pathlib; "
            f"sys.exit(0 if not storage.DataLock(pathlib.Path(r'{tmp_path / 'bot.lock'}')).acquire() else 1)")
    out = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(ROOT)})
    lock.release()
    assert out.returncode == 0


def test_second_instance_exits_cleanly(tmp_path):
    """Start bot.py while its data folder is locked: it must log a clear
    message and exit with code 3 without touching the data."""
    import socket
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    data, logs = tmp_path / "data", tmp_path / "logs"
    data.mkdir()
    (data / "jobs.json").write_text("[]")
    holder = storage.DataLock(data / "bot.lock")
    assert holder.acquire()
    try:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "DISCORD_TOKEN": "x.y.z",
               "LOCK_PORT": str(port), "ZM_DATA_DIR": str(data), "ZM_LOG_DIR": str(logs)}
        out = subprocess.run([sys.executable, str(ROOT / "bot.py")], cwd=tmp_path,
                             capture_output=True, text=True, env=env, timeout=60)
    finally:
        holder.release()
    assert out.returncode == 3, out.stdout + out.stderr
    assert "Another copy of this bot is already using" in out.stdout
    assert "Another copy" in (logs / "bot.log").read_text(encoding="utf-8")
    assert (data / "jobs.json").read_text() == "[]"


def test_port_lock_blocks_a_second_copy(tmp_path):
    import socket
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        env = {**os.environ, "PYTHONPATH": str(ROOT), "DISCORD_TOKEN": "x.y.z",
               "LOCK_PORT": str(port), "ZM_DATA_DIR": str(tmp_path / "d"),
               "ZM_LOG_DIR": str(tmp_path / "l")}
        out = subprocess.run([sys.executable, str(ROOT / "bot.py")], cwd=tmp_path,
                             capture_output=True, text=True, env=env, timeout=60)
    finally:
        held.close()
    assert out.returncode == 3
    assert "already running on this machine" in out.stdout


def test_missing_token_exits_with_a_clear_message(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "DISCORD_TOKEN": "",
           "ZM_DATA_DIR": str(tmp_path / "d"), "ZM_LOG_DIR": str(tmp_path / "l")}
    out = subprocess.run([sys.executable, str(ROOT / "bot.py")], cwd=tmp_path,
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 2 and "DISCORD_TOKEN is missing" in out.stdout
