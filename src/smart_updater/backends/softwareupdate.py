"""macOS system updates via ``softwareupdate`` (Command Line Tools, Safari, XProtect, ... and macOS itself).

EXPERIMENTAL: written against the documented output of ``softwareupdate --list``, with the parser
tested against fixtures, but not yet run on a real Mac.

This backend is deliberately cautious:
  * macOS releases themselves (labels starting "macOS") are never installed unattended; they need
    a restart, cannot be uninstalled and, on Apple silicon, an administrator's credentials.
  * Any update Apple marks as needing a restart or shutdown is left for a person, because Smart
    Updater never reboots.
  * There is no dry run and no rollback. Everything it does apply is a small, restart-free update.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List

from ..models import ApplyResult, SimResult, Update
from .base import Backend, BackendError

_SOFTWAREUPDATE = "/usr/sbin/softwareupdate"
_LABEL = re.compile(r"^\s*\*\s*Label:\s*(?P<label>.+?)\s*$")
_TITLE = re.compile(r"Title:\s*(?P<title>.*?),\s*Version:\s*(?P<version>.*?),\s*Size:\s*(?P<size>[^,]+),(?P<rest>.*)$")
_ACTION = re.compile(r"Action:\s*(?P<action>[A-Za-z][A-Za-z ]*?)\s*(?:,|$)")
_NONE = re.compile(r"No new software available", re.I)


def parse_list(text: str, backend_id: str = "softwareupdate") -> List[Update]:
    """Parse ``softwareupdate --list`` (macOS 10.15 and later format)."""
    if _NONE.search(text):
        return []
    updates: List[Update] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _LABEL.match(line)
        if not m:
            continue
        label = m.group("label")
        detail = lines[i + 1] if i + 1 < len(lines) else ""
        t = _TITLE.search(detail)
        title, version, rest = (t.group("title"), t.group("version"), t.group("rest")) if t else (label, "", detail)
        action = _ACTION.search(rest)
        updates.append(
            Update(
                backend_id, label, "", version or label, display=title.strip() or label,
                source=(action.group("action").strip().lower() if action else ""),
            )
        )
    if not updates and "following" in text.lower():
        raise BackendError("'softwareupdate --list' reported updates but its output was not in a recognised format")
    return updates


class SoftwareUpdateBackend(Backend):
    NAME = "softwareupdate"
    scheme = "generic"
    stable_release = True  # Apple vets these, and the installed version is not reported, so assume small
    rollback_support = "none"
    experimental = True
    can_simulate = False
    default_max_bump = "major"

    def __init__(self, runner, events, options=None):
        super().__init__(runner, events, options)
        self._latest: Dict[str, Update] = {}

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "darwin" and os.path.isfile(_SOFTWAREUPDATE)

    def _list(self) -> List[Update]:
        r = self.run([_SOFTWAREUPDATE, "--list"], label="softwareupdate-list", timeout=600)
        # The tool writes most of its output to stderr, so read both.
        if not r.ok:
            raise BackendError(f"'softwareupdate --list' failed (rc={r.rc}): {r.text.strip()[-300:]}")
        updates = parse_list(r.text, self.id)
        self._latest = {u.name: u for u in updates}
        return updates

    def list_updates(self) -> List[Update]:
        return self._list()

    def manual_reason(self, update: Update) -> str:
        if update.source:
            return (
                f"Apple marks this update as needing a {update.source}, and Smart Updater never restarts the machine. "
                "Install it when the restart is acceptable (System Settings > General > Software Update)."
            )
        return ""

    def hold_hint(self, name: str) -> str:
        return ""

    def simulate(self, targets: List[Update]) -> SimResult:
        return SimResult(
            True,
            upgrades={t.name: t.candidate for t in targets},
            previous={t.name: t.installed for t in targets},
            analyzed=False,
        )

    def installed_versions(self, names: List[str]) -> Dict[str, str]:
        """softwareupdate does not report installed versions. Report the target version for anything
        that is no longer offered (so it counts as installed) and "" for anything still pending."""
        pending = {u.name for u in self._list()}
        return {n: ("" if n in pending else self._remembered(n)) for n in names}

    def _remembered(self, name: str) -> str:
        return getattr(self._latest.get(name), "candidate", "") or "installed"

    def apply(self, sim: SimResult) -> ApplyResult:
        timeout = self.options.get("apply_timeout", 3600)
        known = {n: self._latest.get(n) for n in sim.upgrades}
        failures: List[str] = []
        last_log = ""
        for label in sim.upgrades:
            # No --restart: this backend never reboots, and restart-needing updates are not selected.
            r = self.run(
                [_SOFTWAREUPDATE, "--install", label, "--agree-to-license"],
                label="softwareupdate-install", timeout=timeout, mutating=True,
            )
            last_log = r.log_path or last_log
            if not r.ok:
                failures.append(f"{label}: softwareupdate exited with {r.rc}: {r.text.strip()[-300:]}")
        if not failures:
            still = {u.name for u in self._list()} & set(known)
            if still:
                failures.append(f"softwareupdate reported success but {', '.join(sorted(still))} is still offered as an update")
        return ApplyResult(not failures, 0 if not failures else 1, " | ".join(failures), log_path=last_log)

    def rollback(self, snapshot: Dict[str, Any]) -> ApplyResult:
        names = list(snapshot.get("members", []))
        installed = [n for n, v in self.installed_versions(names).items() if v]
        if not installed:
            return ApplyResult(True, 0, "")  # nothing was installed, so there is nothing to undo
        return ApplyResult(
            False, 1,
            f"macOS system updates cannot be uninstalled by Smart Updater ({', '.join(sorted(installed))} was installed).",
        )
