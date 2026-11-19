"""Backend registry and detection."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .apt import AptBackend
from .base import Backend, BackendError
from .brew import BrewBackend
from .dnf import DnfBackend
from .pip import PipBackend
from .softwareupdate import SoftwareUpdateBackend
from .winget import WingetBackend

SYSTEM_BACKENDS = (AptBackend, DnfBackend, WingetBackend, BrewBackend, SoftwareUpdateBackend)
ALL_NAMES = [c.NAME for c in SYSTEM_BACKENDS] + [PipBackend.NAME]

__all__ = ["Backend", "BackendError", "detect", "ALL_NAMES"]


def _options(cfg, name: str) -> Dict[str, Any]:
    opts = dict(cfg.backend_options(name))
    opts["apply_timeout"] = cfg.apply_timeout
    return opts


def detect(cfg, runner, events, only: Optional[Sequence[str]] = None) -> List[Backend]:
    """Instantiate every backend that is enabled and present on this machine."""
    found: List[Backend] = []
    for cls in SYSTEM_BACKENDS:
        if only and cls.NAME not in only:
            continue
        if not cfg.backend_enabled(cls.NAME, True):
            events.emit("backend_disabled", f"{cls.NAME} disabled in config", level="debug")
            continue
        if cls.available():
            found.append(cls(runner, events, _options(cfg, cls.NAME)))
        else:
            events.emit("backend_unavailable", f"{cls.NAME} not present on this machine", level="debug")
    # pip is opt-in: it only touches environments listed in the config.
    if (not only or "pip" in only) and cfg.backend_enabled("pip", False):
        for env in cfg.backend_options("pip").get("environments", []):
            found.append(PipBackend(runner, events, _options(cfg, "pip"), env))
    return found
