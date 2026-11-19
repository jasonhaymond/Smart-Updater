"""Subprocess runner.

Every external command goes through here so that it is logged, time-limited,
captured to a per-run file, and, importantly, blocked in read-only (plan) mode
if it is marked as mutating.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

TAIL_CHARS = 1500


class ReadOnlyViolation(RuntimeError):
    """A mutating command was attempted during a read-only run."""


@dataclass
class CmdResult:
    cmd: List[str]
    rc: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    log_path: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        return self.stdout + (("\n" + self.stderr) if self.stderr else "")


def _s(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class Runner:
    def __init__(self, events, run_dir: Optional[Path] = None, read_only: bool = True):
        self.events = events
        self.run_dir = Path(run_dir) if run_dir else None
        self.read_only = read_only
        self._n = 0

    def run(
        self,
        cmd: Sequence[str],
        *,
        label: str = "",
        timeout: float = 300,
        env: Optional[Dict[str, str]] = None,
        mutating: bool = False,
    ) -> CmdResult:
        cmd = [str(c) for c in cmd]
        if mutating and self.read_only:
            raise ReadOnlyViolation(f"refusing to run mutating command in read-only mode: {' '.join(cmd)}")
        self._n += 1
        full_env = os.environ.copy()
        full_env.update({"LC_ALL": "C", "LANG": "C"})
        if env:
            full_env.update(env)

        start = time.monotonic()
        timed_out = False
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=full_env,
                stdin=subprocess.DEVNULL,
            )
            rc, out, err = p.returncode, _s(p.stdout), _s(p.stderr)
        except subprocess.TimeoutExpired as e:
            rc, timed_out = 124, True
            out, err = _s(e.stdout), _s(e.stderr) + f"\n[timed out after {timeout}s]"
        except FileNotFoundError:
            rc, out, err = 127, "", f"command not found: {cmd[0]}"
        except OSError as e:
            rc, out, err = 126, "", f"could not run {cmd[0]}: {e}"
        duration = time.monotonic() - start

        log_path = self._save(cmd, label, rc, duration, out, err)
        self.events.emit(
            "cmd",
            " ".join(cmd),
            level="info" if mutating else "debug",
            label=label,
            rc=rc,
            duration_s=round(duration, 2),
            mutating=mutating,
            timed_out=timed_out,
            stdout_tail=out[-TAIL_CHARS:],
            stderr_tail=err[-TAIL_CHARS:],
            log=log_path,
        )
        return CmdResult(cmd, rc, out, err, duration, timed_out, log_path)

    def _save(self, cmd, label, rc, duration, out, err) -> str:
        if not self.run_dir:
            return ""
        d = self.run_dir / "commands"
        d.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (label or Path(cmd[0]).name))
        path = d / f"{self._n:03d}-{safe}.txt"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(cmd)}\nrc={rc} duration={duration:.2f}s\n\n--- stdout ---\n{out}\n--- stderr ---\n{err}\n")
        return str(path)
