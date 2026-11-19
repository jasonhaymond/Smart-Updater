"""Fedora/RHEL family (dnf) backend.

EXPERIMENTAL: the parsers are written against dnf's documented output and
covered by fixture tests, but this backend has not been run on a real
Fedora/RHEL host. Treat plan output as advisory until you have checked it on
your own systems.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any, Dict, List, Set, Tuple

from ..models import ApplyResult, SimResult, Update
from .base import Backend, BackendError

_ARCH = re.compile(r"^(x86_64|noarch|i[3-6]86|aarch64|ppc64le|s390x|armv7hl|riscv64|src)$")
_NEVRA = re.compile(r"^(?P<name>.+)-(?:\d+:)?(?P<ver>[^-]+)-(?P<rel>[^-]+)\.(?P<arch>[A-Za-z0-9_]+)$")
_SECTIONS = {
    "upgrading": "upgrade",
    "installing": "install",
    "installing dependencies": "install",
    "installing weak dependencies": "install",
    "removing": "remove",
    "removing dependent packages": "remove",
    "removing unused dependencies": "remove",
    "downgrading": "remove",  # a downgrade is treated like a removal: never automatic
}


def parse_check_update(text: str, backend_id: str = "dnf") -> List[Update]:
    updates: List[Update] = []
    for line in text.splitlines():
        if line.startswith("Obsoleting Packages"):
            break
        parts = line.split()
        if len(parts) == 3 and "." in parts[0]:
            name, _, arch = parts[0].rpartition(".")
            if _ARCH.match(arch):
                updates.append(Update(backend=backend_id, name=name, installed="", candidate=parts[1], source=parts[2]))
    return updates


def parse_updateinfo(text: str) -> Dict[str, List[str]]:
    """Map package name -> advisory ids, from ``dnf updateinfo list --security``."""
    out: Dict[str, List[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        m = _NEVRA.match(parts[-1])
        if m:
            out.setdefault(m.group("name"), []).append(parts[0])
    return out


def parse_transaction(text: str) -> Dict[str, Any]:
    upgrades: Dict[str, str] = {}
    previous: Dict[str, str] = {}
    installs: Dict[str, str] = {}
    removals: List[str] = []
    section = None
    last_upgraded = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("Transaction Summary"):
            break
        key = stripped.rstrip(":").lower()
        if stripped.endswith(":") and key in _SECTIONS:
            section, last_upgraded = _SECTIONS[key], None
            continue
        if section is None:
            continue
        parts = stripped.split()
        if len(parts) >= 4 and parts[0] == "replacing" and _ARCH.match(parts[2]) and last_upgraded:
            previous[last_upgraded] = parts[3]
            continue
        if len(parts) >= 3 and _ARCH.match(parts[1]):
            name, ver = parts[0], parts[2]
            if section == "upgrade":
                upgrades[name], last_upgraded = ver, name
            elif section == "install":
                installs[name] = ver
            else:
                removals.append(name)
    return {"upgrades": upgrades, "previous": previous, "installs": installs, "removals": removals}


class DnfBackend(Backend):
    NAME = "dnf"
    scheme = "rpm"
    stable_release = True
    rollback_support = "best_effort"
    experimental = True
    default_max_bump = "major"

    @classmethod
    def available(cls) -> bool:
        return os.name != "nt" and bool(shutil.which("dnf")) and bool(shutil.which("rpm"))

    def refresh(self) -> bool:
        return self.run(["dnf", "-q", "makecache"], label="dnf-makecache", timeout=900).ok

    def list_updates(self) -> List[Update]:
        r = self.run(["dnf", "-q", "check-update"], label="dnf-check-update", timeout=600)
        if r.rc not in (0, 100):
            raise BackendError(f"dnf check-update failed (rc={r.rc}): {r.stderr.strip()[-300:]}")
        updates = parse_check_update(r.stdout, self.id)
        if not updates:
            return []
        sec = self.run(["dnf", "-q", "updateinfo", "list", "--security"], label="dnf-updateinfo", timeout=600)
        advisories = parse_updateinfo(sec.stdout) if sec.ok else {}
        installed = self.installed_versions([u.name for u in updates])
        for u in updates:
            u.installed = installed.get(u.name, "")
            if u.name in advisories:
                u.security, u.advisories = True, advisories[u.name]
        return updates

    def held(self) -> Set[str]:
        r = self.run(["dnf", "-q", "versionlock", "list"], label="dnf-versionlock")
        names: Set[str] = set()
        if r.ok:
            for line in r.stdout.splitlines():
                # versionlock lines look like "name-0:1.2.3-4.fc40.*"; drop the epoch, then split off name
                m = re.match(r"^(?P<name>.+)-[^-]+-[^-]+\.[^.]+$", re.sub(r"-\d+:", "-", line.strip()))
                if m:
                    names.add(m.group("name"))
        return names

    def hold_hint(self, name: str) -> str:
        return f"dnf versionlock delete {name}"

    def simulate(self, targets: List[Update]) -> SimResult:
        specs = [f"{t.name}-{t.candidate}" for t in targets]
        r = self.run(["dnf", "upgrade", "--assumeno", "--best", *specs], label="dnf-simulate", timeout=600)
        text = r.text
        if "Problem" in text or "Error:" in text or "Transaction Summary" not in text:
            blockers = re.findall(r"package (\S+?)-\d", text)
            return SimResult(False, error=text.strip()[-700:], blockers=blockers)
        parsed = parse_transaction(r.stdout)
        missing = [t.name for t in targets if t.name not in parsed["upgrades"]]
        if missing:
            return SimResult(False, error=f"dnf did not plan an upgrade for {', '.join(missing)}", **parsed)
        return SimResult(True, **parsed)

    def installed_versions(self, names: List[str]) -> Dict[str, str]:
        if not names:
            return {}
        r = self.run(["rpm", "-q", "--qf", "%{NAME} %{EPOCH}:%{VERSION}-%{RELEASE}\\n", *names], label="rpm-query")
        found: Dict[str, str] = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                epoch, _, rest = parts[1].partition(":")
                found[parts[0]] = rest if epoch in ("(none)", "0") else parts[1]
        return found

    def problems(self) -> List[str]:
        r = self.run(["dnf", "-q", "check"], label="dnf-check", timeout=300)
        return [] if r.ok else [l.strip() for l in r.text.splitlines() if l.strip()]

    def reboot_required(self) -> List[str]:
        r = self.run(["dnf", "-q", "needs-restarting", "-r"], label="dnf-needs-restarting")
        return ["needs-restarting reports a reboot is required"] if r.rc == 1 else []

    def _history_id(self) -> str:
        r = self.run(["dnf", "-q", "history", "list"], label="dnf-history")
        for line in r.stdout.splitlines():
            m = re.match(r"^\s*(\d+)\s*\|", line)
            if m:
                return m.group(1)
        return ""

    def apply(self, sim: SimResult) -> ApplyResult:
        specs = [f"{n}-{v}" for n, v in sim.upgrades.items()]
        r = self.run(["dnf", "-y", "upgrade", "--best", *specs], label="dnf-apply", timeout=self.options.get("apply_timeout", 3600), mutating=True)
        token = self._history_id() if r.ok else None
        return ApplyResult(r.ok, r.rc, "" if r.ok else r.text.strip()[-500:], token=token, log_path=r.log_path)

    def rollback(self, snapshot: Dict[str, Any]) -> ApplyResult:
        timeout = self.options.get("apply_timeout", 3600)
        if snapshot.get("token"):
            r = self.run(["dnf", "-y", "history", "undo", snapshot["token"]], label="dnf-undo", timeout=timeout, mutating=True)
        else:
            specs = [f"{n}-{v}" for n, v in snapshot.get("versions", {}).items() if v]
            if not specs:
                return ApplyResult(True)
            r = self.run(["dnf", "-y", "downgrade", *specs], label="dnf-downgrade", timeout=timeout, mutating=True)
        return ApplyResult(r.ok, r.rc, "" if r.ok else r.text.strip()[-500:], log_path=r.log_path)
