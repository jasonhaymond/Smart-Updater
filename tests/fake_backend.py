"""A tiny simulated package world for testing the planner and executor.

It models just enough of what apt does: upgrading a package can pull in newer
versions of what it requires, and can conflict with an installed package, which
apt would resolve by *removing* it unless that package is upgraded in the same
transaction.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set

from smart_updater.backends.base import Backend
from smart_updater.health import Health, HealthSnapshot
from smart_updater.models import ApplyResult, SimResult, Update


class NullEvents:
    def emit(self, *a, **k):
        pass


class FakeBackend(Backend):
    NAME = "fake"
    scheme = "deb"
    stable_release = True
    rollback_support = "best_effort"

    def __init__(
        self,
        installed: Dict[str, str],
        available: Dict[str, str],
        requires: Optional[Dict[str, List[str]]] = None,
        conflicts: Optional[Dict[str, List[str]]] = None,
        needs_explicit: Optional[Dict[str, List[str]]] = None,
        held: Set[str] = frozenset(),
        fail_apply: Set[str] = frozenset(),
        breaking: Set[tuple] = frozenset(),
        rollback_fails: bool = False,
        security: Set[str] = frozenset(),
    ):
        super().__init__(None, NullEvents(), {})
        self.installed = dict(installed)
        self.available = dict(available)
        self.requires = requires or {}
        self.conflicts = conflicts or {}
        self.needs_explicit = needs_explicit or {}
        self.held_names = set(held)
        self.fail_apply = set(fail_apply)
        self.breaking = set(breaking)  # (name, version) pairs that make the service unhealthy
        self.rollback_fails = rollback_fails
        self.security = set(security)
        self.apply_calls: List[Dict[str, str]] = []
        self.rollback_calls: List[Dict[str, str]] = []

    @classmethod
    def available_here(cls):
        return True

    def list_updates(self) -> List[Update]:
        return [
            Update(self.id, n, self.installed[n], v, security=n in self.security)
            for n, v in self.available.items()
            if n in self.installed and self.installed[n] != v
        ]

    def held(self):
        return set(self.held_names)

    def hold_hint(self, name):
        return f"fake-unhold {name}"

    def simulate(self, targets: List[Update]) -> SimResult:
        up = {t.name: t.candidate for t in targets}
        for n in list(up):
            missing = [d for d in self.needs_explicit.get(n, []) if d not in up]
            if missing:
                return SimResult(False, error=f"{n} : Depends: {missing[0]} (>= new) but the old one is installed", blockers=[n, *missing])
        queue = list(up)
        while queue:
            n = queue.pop()
            for dep in self.requires.get(n, []):
                if dep in up:
                    continue
                if dep in self.available:
                    up[dep] = self.available[dep]
                    queue.append(dep)
                else:
                    return SimResult(False, error=f"{n} : Depends: {dep} but it is not installable", blockers=[n, dep])
        removals = [c for n in up for c in self.conflicts.get(n, []) if c not in up and c in self.installed]
        previous = {n: self.installed[n] for n in up if n in self.installed}
        return SimResult(True, upgrades=up, previous=previous, removals=removals)

    def apply(self, sim: SimResult) -> ApplyResult:
        self.apply_calls.append(dict(sim.upgrades))
        if self.fail_apply & set(sim.upgrades):
            return ApplyResult(False, 1, "boom")
        self.installed.update(sim.upgrades)
        return ApplyResult(True)

    def rollback(self, snapshot) -> ApplyResult:
        self.rollback_calls.append(dict(snapshot["versions"]))
        if self.rollback_fails:
            return ApplyResult(False, 1, "cannot downgrade")
        self.installed.update(snapshot["versions"])
        return ApplyResult(True)

    def installed_versions(self, names):
        return {n: self.installed[n] for n in names if n in self.installed}

    def healthy(self) -> bool:
        return not any((n, v) in self.breaking for n, v in self.installed.items())


class FakeHealth:
    """Health that reads the fake world, with the real regression logic."""

    def __init__(self, backend: FakeBackend):
        self.b = backend

    def snapshot(self) -> HealthSnapshot:
        return HealthSnapshot(custom={"service": self.b.healthy()})

    regressions = staticmethod(Health.regressions)
