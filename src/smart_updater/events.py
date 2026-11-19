"""Run logging.

Every run produces three things:
  * ``smart-updater.log``      human-readable rolling text log (all runs)
  * ``runs/<id>/events.jsonl`` one JSON object per event, with full detail
  * ``runs/<id>/commands/``    the complete output of every command that ran
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class EventLog:
    def __init__(self, state_dir, run_id: str, verbose: bool = False, quiet: bool = False):
        self.run_id = run_id
        self.run_dir = Path(state_dir) / "runs" / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl = open(self.run_dir / "events.jsonl", "a", encoding="utf-8")

        self.logger = logging.getLogger(f"smart_updater.run.{run_id}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self._handlers = []

        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
        fh = RotatingFileHandler(
            Path(state_dir) / "smart-updater.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG if verbose else logging.INFO)
        fh.setFormatter(fmt)
        self._add(fh)

        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO))
        ch.setFormatter(logging.Formatter("%(message)s"))
        self._add(ch)

    def _add(self, handler) -> None:
        self.logger.addHandler(handler)
        self._handlers.append(handler)

    def emit(self, event: str, msg: str = "", level: str = "info", **data) -> None:
        rec = {"ts": now_iso(), "run": self.run_id, "level": level, "event": event, "msg": msg}
        rec.update(data)
        self._jsonl.write(json.dumps(rec, default=str) + "\n")
        self._jsonl.flush()
        scalars = " ".join(
            f"{k}={v}" for k, v in data.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) < 200 and v != ""
        )
        text = f"[{event}] {msg}" + (f" ({scalars})" if scalars and event != "cmd" else "")
        if event == "cmd":
            text = f"[cmd] {msg} -> rc={data.get('rc')} in {data.get('duration_s')}s"
        self.logger.log(_LEVELS.get(level, logging.INFO), text)

    def close(self) -> None:
        for h in self._handlers:
            self.logger.removeHandler(h)
            h.close()
        self._handlers = []
        try:
            self._jsonl.close()
        except Exception:
            pass
