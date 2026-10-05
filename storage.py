#!/usr/bin/env python3
"""
JSON persistence for the Zade Meadows monitor.

A normal open(..., "w") truncates the file first. If the process dies in that
window (PC sleeps, power cut, Ctrl-C) you are left with a half-written or
empty jobs.json. Everything here avoids that:

  write:  serialise -> temp file -> flush + fsync -> copy current file to
          .bak -> atomic replace -> fsync the folder
  read:   primary -> .bak. A file that does not parse, or parses to the
          wrong shape, is moved aside as *.corrupt-<time> (never deleted)
          so the next save cannot rotate it over the good backup.

At every instant there is a complete primary file on disk; a crash leaves
either the old version or the new one, never a partial one.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("zm.storage")


class PersistenceError(Exception):
    """A save did not reach the disk. The caller must not assume it did."""


def backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".bak")


def _quarantine(path: Path) -> Optional[Path]:
    target = path.with_name(f"{path.stem}.corrupt-{int(time.time())}{path.suffix}")
    try:
        path.replace(target)
        log.error("Moved damaged %s aside as %s (kept, not deleted).", path.name, target.name)
        return target
    except Exception as exc:
        log.error("Could not move damaged %s aside: %s", path.name, exc)
        return None


def read_json(path: Path, default: Any,
              validate: Optional[Callable[[Any], bool]] = None) -> Any:
    """Load path, falling back to its .bak. Returns `default` only when
    neither copy is usable - and in that case the damaged primary is kept
    on disk under a .corrupt- name for manual recovery."""
    primary_damaged = False
    for candidate in (path, backup_path(path)):
        if not candidate.exists():
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if validate is not None and not validate(data):
                raise ValueError(f"unexpected content (top level is {type(data).__name__})")
        except Exception as exc:
            log.error("Could not read %s (%s: %s)", candidate.name, type(exc).__name__, exc)
            if candidate == path:
                primary_damaged = True
            continue

        if primary_damaged:
            # The good data came from .bak. Move the broken primary out of the
            # way now, or the next save would rotate it over that backup.
            _quarantine(path)
            log.warning("%s was unreadable; recovered from %s.", path.name, candidate.name)
        return data

    if primary_damaged:
        _quarantine(path)
        log.error("No usable copy of %s - starting from an empty state.", path.name)
    return default


def _fsync_dir(folder: Path) -> None:
    if os.name != "posix":
        return  # not supported on Windows; NTFS journals the rename itself
    try:
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _replace(src: Path, dst: Path, attempts: int = 5) -> None:
    """os.replace with a short retry: on Windows an antivirus or indexer
    holding the file open makes the rename fail for a moment."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_json(path: Path, payload: Any) -> None:
    """Atomically replace `path` with `payload`. Raises PersistenceError if
    the new content did not reach the disk; the old file is then untouched."""
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(f"{path.name}: data is not serialisable: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        if path.exists():
            # Copy (not move) the current version to .bak, itself atomically,
            # so a complete primary exists at every moment.
            bak_tmp = path.with_name(f"{path.name}.{os.getpid()}.baktmp")
            try:
                with open(path, "rb") as src, open(bak_tmp, "wb") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
                _replace(bak_tmp, backup_path(path))
            except Exception as exc:
                log.warning("Could not refresh %s: %s", backup_path(path).name, exc)
            finally:
                bak_tmp.unlink(missing_ok=True)

        _replace(tmp, path)
        _fsync_dir(path.parent)
    except Exception as exc:
        raise PersistenceError(f"Failed to save {path.name}: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Data-folder lock: one process per data folder, whatever the port setting.
# --------------------------------------------------------------------------

class DataLock:
    """An OS-level exclusive lock on <data>/bot.lock. Released automatically
    if the process dies, so a crash never leaves a stale lock behind."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
