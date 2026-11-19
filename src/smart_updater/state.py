"""State directory, run lock, and the small amount of memory kept between runs."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict


class LockError(RuntimeError):
    pass


def is_privileged() -> bool:
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def is_macos() -> bool:
    return sys.platform == "darwin"


MAC_SYSTEM_STATE = "/Library/Application Support/SmartUpdater"


def _mac_user_state() -> Path:
    return Path.home() / "Library" / "Application Support" / "SmartUpdater"


def default_state_dir() -> Path:
    override = os.environ.get("SMART_UPDATER_STATE_DIR")
    if override:
        return Path(override)
    if is_macos():
        return Path(MAC_SYSTEM_STATE) if is_privileged() else _mac_user_state()
    if os.name == "nt":
        if is_privileged():
            return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SmartUpdater"
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "SmartUpdater"
    if is_privileged():
        return Path("/var/lib/smart-updater")
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "smart-updater"


def candidate_state_dirs() -> list:
    """Every place a report may live, most likely first. Readers use the first that has one."""
    override = os.environ.get("SMART_UPDATER_STATE_DIR")
    if override:
        return [Path(override)]
    if is_macos():
        return [Path(MAC_SYSTEM_STATE), _mac_user_state()]
    if os.name == "nt":
        return [
            Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SmartUpdater",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "SmartUpdater",
        ]
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return [Path("/var/lib/smart-updater"), Path(base) / "smart-updater"]


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """Exclusive lock file so two applying runs never overlap."""

    def __init__(self, path: Path, stale_after: float = 6 * 3600):
        self.path = Path(path)
        self.stale_after = stale_after

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fh:
                    fh.write(f"{os.getpid()} {int(time.time())}\n")
                return self
            except FileExistsError:
                pid, ts = self._read()
                age = time.time() - ts
                if not pid_alive(pid) or age > self.stale_after:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise LockError(f"another Smart Updater run is active (pid {pid}, started {int(age)}s ago)")
        raise LockError("could not acquire the run lock")

    def _read(self):
        try:
            a, b = self.path.read_text().split()[:2]
            return int(a), int(b)
        except Exception:
            return 0, 0

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class State:
    """Layout of the state directory plus the cross-run memory in state.json."""

    DEFAULTS: Dict[str, Any] = {"first_seen": {}, "quarantine": {}, "backend_errors": {}, "last_run": None}

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o755)  # so an unprivileged monitoring agent can read the reports
        except OSError:
            pass
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(exist_ok=True)
        self.latest_json = self.root / "latest.json"
        self.latest_txt = self.root / "latest.txt"
        self.lock_path = self.root / "run.lock"
        self._path = self.root / "state.json"
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        data = {k: (v.copy() if isinstance(v, dict) else v) for k, v in self.DEFAULTS.items()}
        try:
            data.update(json.loads(self._path.read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            pass
        return data

    def save(self) -> None:
        atomic_write_text(self._path, json.dumps(self.data, indent=2, sort_keys=True), mode=0o600)

    def prune_runs(self, keep: int) -> None:
        runs = sorted(p for p in self.runs_dir.iterdir() if p.is_dir())
        for p in runs[: max(0, len(runs) - keep)]:
            shutil.rmtree(p, ignore_errors=True)
