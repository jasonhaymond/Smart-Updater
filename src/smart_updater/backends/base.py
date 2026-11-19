"""Backend interface.

A backend wraps one package manager. The planner and executor only talk to this
interface, which is what lets the same "simulate, resolve, verify, roll back"
logic drive apt, dnf, winget, Homebrew, macOS softwareupdate and pip.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from ..models import ApplyResult, SimResult, Update


class BackendError(RuntimeError):
    """The package manager could not be queried or behaved unexpectedly."""


class Backend:
    NAME = "base"
    scheme = "generic"  # version scheme, see version.py
    stable_release = False  # True when a distro vets versions per release (apt/dnf)
    rollback_support = "none"  # none | best_effort
    experimental = False
    can_simulate = True
    default_max_bump = "major"
    osv_ecosystem: Optional[str] = None

    def __init__(self, runner, events, options: Optional[Dict[str, Any]] = None):
        self.runner = runner
        self.events = events
        self.options = options or {}

    # ---- identity --------------------------------------------------------
    @property
    def id(self) -> str:
        return self.NAME

    @classmethod
    def available(cls) -> bool:
        raise NotImplementedError

    def norm(self, name: str) -> str:
        """Normalise a package name for matching against error text."""
        return name

    def describe(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "rollback_support": self.rollback_support,
            "can_simulate": self.can_simulate,
            "experimental": self.experimental,
        }

    def run(self, cmd, **kw):
        return self.runner.run(cmd, **kw)

    # ---- read-only queries -----------------------------------------------
    def refresh(self) -> bool:
        """Refresh package metadata. Does not change installed software."""
        return True

    def list_updates(self) -> List[Update]:
        raise NotImplementedError

    def held(self) -> Set[str]:
        return set()

    def hold_hint(self, name: str) -> str:
        return ""

    def manual_reason(self, update: Update) -> str:
        """A reason this particular update needs a person, or "" if the backend has no objection.

        For things only the backend can know (for example macOS marking an update as needing a restart).
        """
        return ""

    def simulate(self, targets: List[Update]) -> SimResult:
        raise NotImplementedError

    def installed_versions(self, names: List[str]) -> Dict[str, str]:
        raise NotImplementedError

    def installed_all(self) -> Dict[str, str]:
        return {}

    def problems(self) -> List[str]:
        """Package-database problems (broken deps, half-configured packages)."""
        return []

    def reboot_required(self) -> List[str]:
        return []

    # ---- changes ---------------------------------------------------------
    def apply(self, sim: SimResult) -> ApplyResult:
        raise NotImplementedError

    def snapshot(self, sim: SimResult) -> Dict[str, Any]:
        """Record what is needed to undo this change."""
        previous = dict(sim.previous)
        missing = [n for n in sim.upgrades if n not in previous]
        if missing:
            previous.update(self.installed_versions(missing))
        return {"backend": self.id, "versions": previous, "token": None}

    def rollback(self, snapshot: Dict[str, Any]) -> ApplyResult:
        raise NotImplementedError
