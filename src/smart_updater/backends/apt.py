"""Debian/Ubuntu (apt) backend. Verified against a real apt dry run."""
from __future__ import annotations

import os
import re
import shutil
from typing import Any, Dict, List, Set

from ..models import ApplyResult, SimResult, Update
from .base import Backend, BackendError

_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "NEEDRESTART_MODE": "l",
    "APT_LISTCHANGES_FRONTEND": "none",
    "UCF_FORCE_CONFFOLD": "1",
}
_UPGRADABLE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+(?P<cand>\S+)\s+(?P<arch>\S+)\s+\[upgradable from: (?P<old>[^\]]+)\]"
)
_INST = re.compile(r"^Inst (?P<name>\S+)(?: \[(?P<old>[^\]]+)\])? \((?P<new>\S+) ")
_REMV = re.compile(r"^Remv (?P<name>\S+)")
_DEPLINE = re.compile(r"^\s+(?P<pkg>\S+) : (?:PreDepends|Depends|Breaks|Conflicts): (?P<dep>\S+)")


def _key(name: str, arch: str, native: str) -> str:
    return name if arch in (native, "all", "") else f"{name}:{arch}"


def _strip_native(name: str, native: str) -> str:
    return name[: -(len(native) + 1)] if native and name.endswith(f":{native}") else name


def parse_upgradable(text: str, native: str, backend_id: str = "apt") -> List[Update]:
    updates: List[Update] = []
    for line in text.splitlines():
        m = _UPGRADABLE.match(line)
        if not m:
            continue
        suites = m.group("suites")
        updates.append(
            Update(
                backend=backend_id,
                name=_key(m.group("name"), m.group("arch"), native),
                installed=m.group("old"),
                candidate=m.group("cand"),
                security="security" in suites.lower(),
                source=suites,
            )
        )
    return updates


def parse_simulation(text: str, native: str) -> Dict[str, Any]:
    upgrades: Dict[str, str] = {}
    previous: Dict[str, str] = {}
    installs: Dict[str, str] = {}
    removals: List[str] = []
    held_back: List[str] = []
    in_kept = False
    for line in text.splitlines():
        if line.startswith("The following packages have been kept back"):
            in_kept = True
            continue
        if in_kept:
            if line.startswith("  "):
                held_back += [_strip_native(n, native) for n in line.split()]
                continue
            in_kept = False
        m = _INST.match(line)
        if m:
            name = _strip_native(m.group("name"), native)
            if m.group("old"):
                upgrades[name] = m.group("new")
                previous[name] = m.group("old")
            else:
                installs[name] = m.group("new")
            continue
        m = _REMV.match(line)
        if m:
            removals.append(_strip_native(m.group("name"), native))
    return {"upgrades": upgrades, "previous": previous, "installs": installs, "removals": removals, "held_back": held_back}


def parse_errors(text: str):
    errs = [l.strip() for l in text.splitlines() if l.startswith("E:")]
    deps = [l.strip() for l in text.splitlines() if _DEPLINE.match(l)]
    blockers: List[str] = []
    for l in text.splitlines():
        m = _DEPLINE.match(l)
        if m:
            blockers += [m.group("pkg"), m.group("dep")]
    msg = "; ".join(deps + errs)[:700] or text.strip()[-400:]
    return msg, blockers


class AptBackend(Backend):
    NAME = "apt"
    scheme = "deb"
    stable_release = True
    rollback_support = "best_effort"  # old versions must still be in the cache or a repo
    default_max_bump = "major"

    def __init__(self, runner, events, options=None):
        super().__init__(runner, events, options)
        self._native = ""

    @classmethod
    def available(cls) -> bool:
        return os.name != "nt" and bool(shutil.which("apt-get")) and bool(shutil.which("dpkg-query"))

    @property
    def native(self) -> str:
        if not self._native:
            r = self.run(["dpkg", "--print-architecture"], label="dpkg-arch")
            self._native = r.stdout.strip() if r.ok else ""
        return self._native

    # ---- queries ---------------------------------------------------------
    def refresh(self) -> bool:
        r = self.run(["apt-get", "update", "-q", "-o", "DPkg::Lock::Timeout=60"], label="apt-update", timeout=900, env=_ENV)
        return r.ok

    def list_updates(self) -> List[Update]:
        r = self.run(["apt", "list", "--upgradable"], label="apt-list-upgradable", timeout=300, env=_ENV)
        if not r.ok:
            raise BackendError(f"apt list --upgradable failed (rc={r.rc}): {r.stderr.strip()[-300:]}")
        return parse_upgradable(r.stdout, self.native, self.id)

    def held(self) -> Set[str]:
        r = self.run(["apt-mark", "showhold"], label="apt-showhold")
        names: Set[str] = set()
        if r.ok:
            for n in r.stdout.split():
                names.add(n)
                names.add(_strip_native(n, self.native))
        return names

    def hold_hint(self, name: str) -> str:
        return f"apt-mark unhold {name}"

    def simulate(self, targets: List[Update]) -> SimResult:
        specs = [f"{t.name}={t.candidate}" for t in targets]
        r = self.run(
            ["apt-get", "-s", "-o", "Debug::NoLocking=1", "install", "--only-upgrade", *specs],
            label="apt-simulate",
            timeout=300,
            env=_ENV,
        )
        parsed = parse_simulation(r.stdout, self.native)
        if not r.ok:
            msg, blockers = parse_errors(r.text)
            return SimResult(False, error=msg, blockers=blockers, **parsed)
        missing = [t.name for t in targets if t.name not in parsed["upgrades"]]
        if missing:
            return SimResult(
                False,
                error=f"apt did not plan an upgrade for {', '.join(missing)} (held back, or already at that version)",
                blockers=parsed["held_back"],
                **parsed,
            )
        return SimResult(True, **parsed)

    def installed_versions(self, names: List[str]) -> Dict[str, str]:
        r = self.run(
            ["dpkg-query", "-W", "-f=${Package} ${Architecture} ${Version} ${db:Status-Abbrev}\\n"],
            label="dpkg-query",
        )
        wanted = set(names)
        found: Dict[str, str] = {}
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4 or not parts[3].startswith("ii"):
                continue
            key = _key(parts[0], parts[1], self.native)
            if key in wanted:
                found[key] = parts[2]
        return found

    def problems(self) -> List[str]:
        lines: List[str] = []
        r = self.run(["dpkg", "--audit"], label="dpkg-audit")
        lines += [l.strip() for l in r.stdout.splitlines() if l.strip()]
        r2 = self.run(["apt-get", "check", "-q"], label="apt-check", env=_ENV)
        if not r2.ok:
            lines += [f"apt-get check: {l.strip()}" for l in r2.text.splitlines() if l.startswith("E:")] or ["apt-get check failed"]
        return lines

    def reboot_required(self) -> List[str]:
        for flag in ("/var/run/reboot-required", "/run/reboot-required"):
            if os.path.exists(flag):
                pkgs = []
                try:
                    with open(flag + ".pkgs", encoding="utf-8") as fh:
                        pkgs = [l.strip() for l in fh if l.strip()]
                except OSError:
                    pass
                return pkgs or ["(package list unavailable)"]
        return []

    # ---- changes ---------------------------------------------------------
    def _install_cmd(self, specs: List[str], downgrade: bool) -> List[str]:
        cmd = ["apt-get", "install", "-y", "--no-remove"]
        cmd.append("--allow-downgrades" if downgrade else "--only-upgrade")
        cmd += ["-o", "Dpkg::Options::=--force-confold", "-o", "Dpkg::Options::=--force-confdef", "-o", "DPkg::Lock::Timeout=300"]
        return cmd + specs

    def apply(self, sim: SimResult) -> ApplyResult:
        specs = [f"{n}={v}" for n, v in sim.upgrades.items()]
        r = self.run(
            self._install_cmd(specs, False),
            label="apt-apply",
            timeout=self.options.get("apply_timeout", 3600),
            env=_ENV,
            mutating=True,
        )
        return ApplyResult(r.ok, r.rc, "" if r.ok else parse_errors(r.text)[0], log_path=r.log_path)

    def rollback(self, snapshot: Dict[str, Any]) -> ApplyResult:
        specs = [f"{n}={v}" for n, v in snapshot.get("versions", {}).items() if v]
        if not specs:
            return ApplyResult(True)
        r = self.run(
            self._install_cmd(specs, True),
            label="apt-rollback",
            timeout=self.options.get("apply_timeout", 3600),
            env=_ENV,
            mutating=True,
        )
        return ApplyResult(r.ok, r.rc, "" if r.ok else parse_errors(r.text)[0], log_path=r.log_path)
