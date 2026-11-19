"""Optional: tell Looksee about each run by calling a Heartbeat check's URL.

Looksee gives every Heartbeat (and Push value) check a private URL, ``https://<looksee>/api/hb/<token>``.
Calling it records a result: ``status`` up/warn/down, a ``msg`` and a numeric ``value``. If the calls stop
arriving, Looksee marks the check down, which is exactly "the updater silently stopped running".

The URL is the only credential, so it is never logged, never printed and never put on a command line.
It is read from the environment variable ``SMART_UPDATER_LOOKSEE_URL`` or from a file named in the config
(``[looksee] push_url_file``); keep that file readable by root only.

Nothing here can affect an update run: a failed push is a warning in the log and nothing else.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from . import __version__
from .report import check_report

ENV_URL = "SMART_UPDATER_LOOKSEE_URL"
_STATUS = {0: "up", 1: "warn", 2: "down"}
_MAX_MSG = 900  # Looksee keeps 1000 characters


def load_url(cfg=None, url_file: Optional[str] = None) -> Optional[str]:
    """The push URL from the environment, ``url_file`` or the config; None when none is set up."""
    env = os.environ.get(ENV_URL, "").strip()
    if env:
        return env
    path = url_file or (getattr(cfg, "looksee", {}) or {}).get("push_url_file")
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ValueError(f"cannot read the Looksee URL file {path}: {e.strerror or e}")
    return text or None


def build_payload(data: Dict[str, Any], max_age_hours: float = 36) -> Dict[str, Any]:
    code, line = check_report(data, max_age_hours)
    message = line.split(" | ", 1)[0]
    att = (data.get("summary") or {}).get("attention") or {}
    payload: Dict[str, Any] = {
        "status": _STATUS.get(code, "warn"),  # unknown (no/stale report) is a warning, never "up"
        "msg": message[:_MAX_MSG],
    }
    if isinstance(att.get("critical"), int) and isinstance(att.get("warning"), int):
        payload["value"] = att["critical"] + att["warning"]
    return payload


def push(url: str, payload: Dict[str, Any], timeout: float = 10) -> Tuple[bool, str]:
    """POST the payload as JSON. Returns (ok, short description) and never raises or reveals the URL."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, "the Looksee URL must start with http:// or https://"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": f"smart-updater/{__version__}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        hint = " (the push URL is wrong or was regenerated in Looksee)" if e.code == 404 else ""
        return False, f"Looksee answered HTTP {e.code}{hint}"
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as e:
        return False, str(getattr(e, "reason", e)).replace(url, "<url>")


def notify(cfg, data: Dict[str, Any], events) -> Optional[bool]:
    """Push the finished run if a URL is configured. True/False = pushed/failed, None = not configured."""
    try:
        url = load_url(cfg)
    except ValueError as e:
        events.emit("looksee_push_failed", str(e), level="warning")
        return False
    if not url:
        return None
    timeout = float((getattr(cfg, "looksee", {}) or {}).get("timeout", 10))
    payload = build_payload(data)
    ok, detail = push(url, payload, timeout)
    host = urlsplit(url).netloc
    if ok:
        events.emit("looksee_push", f"told Looksee at {host}: {payload['status']}", status=payload["status"], detail=detail)
    else:
        events.emit("looksee_push_failed", f"could not tell Looksee at {host}: {detail}", level="warning")
    return ok
