"""Homebrew backend (macOS): formulae and casks.

EXPERIMENTAL: written against Homebrew's documented behaviour, with the output parsers
tested against fixtures, but not yet run on a real Mac.

Things worth knowing:
  * Homebrew refuses to run as root and its files belong to one user. When Smart Updater runs as
    root (as it must to apply updates), it runs every brew command as the owner of the Homebrew
    installation, with ``sudo -n -u <owner>``. Nothing runs as root that the owner could not run.
  * ``brew upgrade --dry-run`` is used for simulation. If the installed Homebrew does not know
    that option the backend says so and falls back to policy-only checks.
  * Homebrew cannot install an older version of a formula or cask, so there is no automatic
    rollback. ``HOMEBREW_NO_INSTALL_CLEANUP`` is set so the previous version normally stays on
    disk, and a failed update is reported as needing attention rather than silently left alone.
    Because of that the default size limit for Homebrew updates is "minor".
  * Casks that update themselves (Chrome, Slack, ...) are skipped by Homebrew unless
    ``greedy_casks = true`` is set; some casks need an administrator's password and will fail
    cleanly when run without one.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from ..models import ApplyResult, SimResult, Update
from .base import Backend, BackendError

BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",  # only the explicit refresh step updates Homebrew itself
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",  # keep the previous version on disk
    "HOMEBREW_NO_ANALYTICS": "1",
    "HOMEBREW_NO_ENV_HINTS": "1",
    "HOMEBREW_NO_EMOJI": "1",
    "NO_COLOR": "1",
}
_ARROW = re.compile(r"^(?P<name>\S+)\s+(?P<old>.+?)\s+->\s+(?P<new>\S+)\s*$")
_NO_DRY_RUN = re.compile(r"unknown (option|switch)|invalid option|unrecognized (option|switch)", re.I)


def find_brew() -> Optional[str]:
    for p in BREW_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # Only trust PATH when not running as root: root must not execute binaries a normal user can replace.
    if os.geteuid() != 0:
        return shutil.which("brew")
    return None


def _last(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[-1]) if value else ""
    return str(value or "")


def parse_outdated(text: str, backend_id: str = "brew") -> Tuple[List[Update], Set[str]]:
    """Parse ``brew outdated --json=v2``. Returns the updates and the names of pinned formulae."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BackendError(f"'brew outdated --json=v2' did not return JSON: {e}")
    updates: List[Update] = []
    pinned: Set[str] = set()
    seen: Set[str] = set()
    for f in data.get("formulae", []):
        name = f.get("name")
        if not name:
            continue
        seen.add(name)
        if f.get("pinned"):
            pinned.add(name)
        updates.append(Update(backend_id, name, _last(f.get("installed_versions")), str(f.get("current_version", "")),
                              display=name, source="formula"))
    for c in data.get("casks", []):
        token = c.get("name")
        if not token:
            continue
        # A formula and a cask can share a name (e.g. "docker"); the cask then gets a prefix so ids stay unique.
        name = f"cask:{token}" if token in seen else token
        updates.append(Update(backend_id, name, _last(c.get("installed_versions")), str(c.get("current_version", "")),
                              display=token, source="cask"))
    return [u for u in updates if u.candidate], pinned


def parse_installed(text: str) -> Dict[str, str]:
    """Parse ``brew info --json=v2 --installed`` into {name: version}, using the same naming as parse_outdated."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BackendError(f"'brew info --json=v2 --installed' did not return JSON: {e}")
    out: Dict[str, str] = {}
    for f in data.get("formulae", []):
        name = f.get("name")
        if not name:
            continue
        kegs = f.get("installed") or []
        version = f.get("linked_keg") or (kegs[-1].get("version") if kegs else "")
        if version:
            out[name] = str(version)
    for c in data.get("casks", []):
        token, version = c.get("token"), c.get("installed")
        if token and version:
            out[f"cask:{token}" if token in out else token] = str(version)
    return out


def parse_dry_run(text: str) -> Dict[str, Tuple[str, str]]:
    """Parse the 'Would upgrade' section of ``brew upgrade --dry-run`` into {name: (old, new)}."""
    found: Dict[str, Tuple[str, str]] = {}
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("==>"):
            in_section = "would upgrade" in stripped.lower()
            continue
        if not in_section:
            continue
        m = _ARROW.match(stripped)
        if m:
            old = m.group("old").split(",")[-1].strip()
            found[m.group("name").split("/")[-1]] = (old, m.group("new"))
    return found


class BrewBackend(Backend):
    NAME = "brew"
    scheme = "generic"
    stable_release = False
    rollback_support = "none"
    experimental = True
    can_simulate = True
    default_max_bump = "minor"
    skipped: List[str] = []

    def __init__(self, runner, events, options=None):
        super().__init__(runner, events, options)
        self.brew = find_brew() or ""
        self._kinds: Dict[str, str] = {}  # name -> "formula" | "cask", learned from the listings

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "darwin" and find_brew() is not None

    # ---- running brew -------------------------------------------------------
    def _owner(self) -> str:
        import pwd  # POSIX only, so imported here: this module is imported on every platform

        try:
            uid = os.stat(os.path.realpath(self.brew)).st_uid
            return pwd.getpwuid(uid).pw_name if uid != 0 else ""
        except (OSError, KeyError):
            return ""

    def describe(self) -> Dict[str, Any]:
        info = super().describe()
        info["path"] = self.brew
        info["runs_as"] = self._owner() or None
        return info

    def _brew(self, args: List[str], label: str, timeout: float = 600, mutating: bool = False):
        argv = [self.brew, *args]
        if os.geteuid() == 0:
            owner = self._owner()
            if not owner:
                raise BackendError(
                    f"{self.brew} is owned by root or by an unknown user. Homebrew will not run as root, "
                    "and Smart Updater only runs it as the user who owns it."
                )
            env_args = [f"{k}={v}" for k, v in _ENV.items()]
            # Start in "/" so a working directory the owner cannot enter does not make brew fail.
            cmd = ["sudo", "-n", "-u", owner, "-H", "/bin/sh", "-c", 'cd / && exec "$@"', "sh", "/usr/bin/env", *env_args, *argv]
            return self.run(cmd, label=label, timeout=timeout, mutating=mutating)
        return self.run(argv, label=label, timeout=timeout, mutating=mutating, env=dict(_ENV))

    # ---- queries ------------------------------------------------------------------
    def refresh(self) -> bool:
        # Homebrew's own package index, not installed software.
        try:
            return self._brew(["update", "--quiet"], "brew-update", timeout=900).ok
        except BackendError as e:
            self.events.emit("refresh_failed", str(e), level="warning")
            return False

    def list_updates(self) -> List[Update]:
        args = ["outdated", "--json=v2"]
        if self.options.get("greedy_casks"):
            args.append("--greedy")
        r = self._brew(args, "brew-outdated")
        if not r.ok:
            raise BackendError(f"'brew outdated' failed (rc={r.rc}): {r.stderr.strip()[-300:] or r.stdout.strip()[-300:]}")
        updates, pinned = parse_outdated(r.stdout, self.id)
        self._pinned = pinned
        for u in updates:
            self._kinds[u.name] = u.source
        return updates

    _pinned: Set[str] = set()

    def held(self) -> Set[str]:
        r = self._brew(["list", "--pinned"], "brew-pinned", timeout=120)
        names = {l.strip() for l in r.stdout.splitlines() if l.strip()} if r.ok else set()
        return names | set(self._pinned)

    def hold_hint(self, name: str) -> str:
        return f"brew unpin {name}"

    def installed_versions(self, names: List[str]) -> Dict[str, str]:
        everything = self.installed_all()
        return {n: everything[n] for n in names if n in everything}

    def installed_all(self) -> Dict[str, str]:
        r = self._brew(["info", "--json=v2", "--installed"], "brew-info-installed", timeout=300)
        if not r.ok:
            raise BackendError(f"'brew info --installed' failed (rc={r.rc}): {r.stderr.strip()[-300:]}")
        return parse_installed(r.stdout)

    def problems(self) -> List[str]:
        # "brew missing": installed formulae whose dependencies are not installed (the Homebrew
        # equivalent of a broken-dependency report). Exits non-zero when it finds any.
        r = self._brew(["missing"], "brew-missing", timeout=300)
        return sorted({l.strip() for l in r.stdout.splitlines() if l.strip()}) if r.rc == 1 else []

    # ---- simulate ---------------------------------------------------------------------
    def _split(self, names: List[str]) -> Tuple[List[str], List[str]]:
        formulae, casks = [], []
        for n in names:
            if self._kinds.get(n) == "cask" or n.startswith("cask:"):
                casks.append(n.split(":", 1)[1] if n.startswith("cask:") else n)
            else:
                formulae.append(n)
        return formulae, casks

    def simulate(self, targets: List[Update]) -> SimResult:
        formulae, casks = self._split([t.name for t in targets])
        upgrades: Dict[str, str] = {}
        previous: Dict[str, str] = {t.name: t.installed for t in targets}
        analyzed = True
        for kind, names in (("formula", formulae), ("cask", casks)):
            if not names:
                continue
            r = self._brew(["upgrade", "--dry-run", f"--{kind}", *names], f"brew-dry-run-{kind}", timeout=600)
            text = r.text
            if not r.ok:
                if _NO_DRY_RUN.search(text):
                    analyzed = False  # this Homebrew has no --dry-run
                    continue
                return SimResult(False, error=text.strip()[-600:] or f"brew exited with {r.rc}")
            parsed = parse_dry_run(text)
            if not parsed:
                analyzed = False  # exit 0 but nothing we could read
                continue
            for name, (old, new) in parsed.items():
                key = f"cask:{name}" if kind == "cask" and f"cask:{name}" in self._kinds else name
                upgrades[key] = new
                previous.setdefault(key, old)
                self._kinds.setdefault(key, kind)
        for t in targets:  # anything the dry run did not mention is still a target
            upgrades.setdefault(t.name, t.candidate)
        return SimResult(True, upgrades=upgrades, previous=previous, analyzed=analyzed)

    # ---- changes -------------------------------------------------------------------------
    def apply(self, sim: SimResult) -> ApplyResult:
        formulae, casks = self._split(list(sim.upgrades))
        timeout = self.options.get("apply_timeout", 3600)
        failures: List[str] = []
        last_log = ""
        for kind, names in (("formula", formulae), ("cask", casks)):
            if not names:
                continue
            r = self._brew(["upgrade", f"--{kind}", *names], f"brew-apply-{kind}", timeout=timeout, mutating=True)
            last_log = r.log_path or last_log
            if not r.ok:
                tail = (r.stderr.strip() or r.stdout.strip())[-400:]
                failures.append(f"brew upgrade --{kind} {' '.join(names)} exited with {r.rc}: {tail}")
        return ApplyResult(not failures, 0 if not failures else 1, " | ".join(failures), log_path=last_log)

    def rollback(self, snapshot: Dict[str, Any]) -> ApplyResult:
        previous = {n: v for n, v in snapshot.get("versions", {}).items() if v}
        now = self.installed_versions(list(previous))
        changed = {n: (previous[n], now.get(n, "?")) for n in previous if now.get(n) != previous[n]}
        if not changed:
            return ApplyResult(True, 0, "")  # nothing was changed, so there is nothing to undo
        detail = ", ".join(f"{n} {old} -> {new}" for n, (old, new) in sorted(changed.items()))
        return ApplyResult(
            False, 1,
            f"Homebrew cannot install an older version automatically ({detail}). The previous version is normally still "
            "in Homebrew's Cellar because Smart Updater never runs 'brew cleanup'.",
        )
